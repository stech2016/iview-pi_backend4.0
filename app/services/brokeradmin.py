"""Broker logins of the customers' gateways, made and removed live by the cloud (Mosquitto Dynamic Security plugin).

  create_plant(plant_id, password)   user name = client id = plant_id; may publish and listen ONLY below
                                     iview/<plant_id>/#  and  spBv1.0/<plant_id>/#
  set_password / set_enabled / delete_plant

The plugin is controlled by MQTT messages on  $CONTROL/dynamic-security/v1  (answers on  .../response). The cloud signs in as the
broker administrator (MQTT_ADMIN_USERNAME / MQTT_ADMIN_PASSWORD from .env) for each call - these calls are rare (add / reset / remove a plant).
"""
import json
import re
import threading
import uuid

import paho.mqtt.client as mqtt

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import get_logger

log = get_logger("brokeradmin")
CONTROL = "$CONTROL/dynamic-security/v1"
PLANT_RE = re.compile(r"^pl[0-9a-f]{10}$")
TIMEOUT = 8.0


def role_name(plant_id: str) -> str:
    return f"plant_{plant_id}"


def _acls(plant_id: str) -> list[dict]:
    topics = [f"iview/{plant_id}/#", f"spBv1.0/{plant_id}/#"]
    out = []
    for topic in topics:
        for acltype in ("publishClientSend", "publishClientReceive", "subscribePattern"):
            out.append({"command": "addRoleACL", "rolename": role_name(plant_id), "acltype": acltype, "topic": topic, "allow": True, "priority": 0})
    return out


def _run(commands: list[dict]) -> list[dict]:
    """Sends the commands in ONE message and returns the plugin's answers (same order). Raises AppError when the broker cannot be reached."""
    if not settings.MQTT_ADMIN_PASSWORD:
        raise AppError(503, "BROKER_ADMIN_NOT_SET", "The broker administrator password (MQTT_ADMIN_PASSWORD) is not set in .env, so plant logins cannot be made yet.")
    done, box = threading.Event(), {}
    cid = f"stech-admin-{uuid.uuid4().hex[:8]}"
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
    c.username_pw_set(settings.MQTT_ADMIN_USERNAME, settings.MQTT_ADMIN_PASSWORD)
    ready = threading.Event()

    def on_connect(client, userdata, flags, rc, props=None):
        if rc != 0:
            box["error"] = f"login refused ({rc})"
            done.set()
            return
        client.subscribe(CONTROL + "/response", qos=1)

    def on_subscribe(client, userdata, mid, granted, props=None):
        if any(getattr(g, "is_failure", False) or g == 0x80 for g in granted):
            box["error"] = "the administrator may not listen for answers"
            done.set()
            return
        ready.set()

    def on_message(client, userdata, msg):
        try:
            box["answers"] = json.loads(msg.payload.decode()).get("responses", [])
        except ValueError:
            box["error"] = "unreadable answer"
        done.set()

    c.on_connect, c.on_subscribe, c.on_message = on_connect, on_subscribe, on_message
    try:
        c.connect(settings.MQTT_HOST, settings.MQTT_PORT, keepalive=20)
        c.loop_start()
        if not ready.wait(TIMEOUT) and not done.is_set():
            raise AppError(503, "BROKER_UNREACHABLE", "The broker did not answer. Try again in a moment.")
        if not done.is_set():
            info = c.publish(CONTROL, json.dumps({"commands": commands}), qos=1)
            info.wait_for_publish(TIMEOUT)
            done.wait(TIMEOUT)
    except AppError:
        raise
    except Exception as exc:
        log.warning("broker_admin_connect_failed", error=type(exc).__name__)
        raise AppError(503, "BROKER_UNREACHABLE", "The broker cannot be reached right now. Try again in a moment.")
    finally:
        try:
            c.loop_stop()
            c.disconnect()
        except Exception:
            pass
    if "error" in box:
        log.warning("broker_admin_failed", detail=box["error"])
        raise AppError(503, "BROKER_ADMIN_FAILED", f"The broker refused the request: {box['error']}.")
    if "answers" not in box:
        raise AppError(503, "BROKER_ADMIN_TIMEOUT", "The broker did not answer in time. Try again in a moment.")
    return box["answers"]


def _check(answers: list[dict], ok_errors: tuple[str, ...] = ()) -> None:
    for a in answers:
        err = a.get("error")
        if err and not any(e in err for e in ok_errors):
            log.warning("broker_admin_error", command=a.get("command"), detail=err)
            raise AppError(502, "BROKER_ADMIN_ERROR", f"The broker said: {err}")


def create_plant(plant_id: str, password: str) -> None:
    """New login (role first, then client). If a half-made one is left from an earlier try it is removed first."""
    if not PLANT_RE.match(plant_id):
        raise AppError(500, "BAD_PLANT_ID", "Internal error: bad plant id.")
    # Safe defaults for every login made by the plugin: nothing is allowed unless the plant's own role says so. (The plugin's start-up
    # default lets a client RECEIVE messages of any topic it manages to subscribe to - the broker accepts such a subscription - so
    # "receive" is switched off here; the plant role then grants receiving of its own two topics only.)
    cmds = [{"command": "setDefaultACLAccess", "acls": [{"acltype": "publishClientSend", "allow": False}, {"acltype": "publishClientReceive", "allow": False},
                                                         {"acltype": "subscribe", "allow": False}, {"acltype": "unsubscribe", "allow": True}]},
            {"command": "deleteClient", "username": plant_id}, {"command": "deleteRole", "rolename": role_name(plant_id)},
            {"command": "createRole", "rolename": role_name(plant_id), "textname": f"Plant {plant_id}"}]
    cmds += _acls(plant_id)
    cmds.append({"command": "createClient", "username": plant_id, "password": password, "clientid": plant_id,
                 "roles": [{"rolename": role_name(plant_id), "priority": 0}], "textname": f"Plant {plant_id}"})
    _check(_run(cmds), ok_errors=("not found",))


def set_password(plant_id: str, password: str) -> None:
    _check(_run([{"command": "setClientPassword", "username": plant_id, "password": password}]))


def set_enabled(plant_id: str, enabled: bool) -> None:
    _check(_run([{"command": "enableClient" if enabled else "disableClient", "username": plant_id}]))


def delete_plant(plant_id: str) -> None:
    _check(_run([{"command": "deleteClient", "username": plant_id}, {"command": "deleteRole", "rolename": role_name(plant_id)}]),
           ok_errors=("not found",))
