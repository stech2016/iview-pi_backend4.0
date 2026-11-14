"""The terms of use the company owner accepts at sign-up. Put your own legal text in a file and set TERMS_FILE=/path/terms.txt in .env
(and TERMS_VERSION when it changes). Without a file, the short default text below is shown."""
import os

from app.core.config import settings

DEFAULT = """STECH iVIEW - Terms of use (default text)

1. You are the owner of this company account and are responsible for the people you create and the rights you give them.
2. You may connect only equipment you are allowed to monitor. Writing values or commands to a plant is your decision and your responsibility.
3. iVIEW does not replace the safety system of your plant. Interlocks and trips stay in the PLC or the safety system.
4. Keep passwords private. Report a lost phone or PC to your Super admin so its access can be stopped.
5. Your plant data stays in your company database until you delete it. Every sign-in, change and command is written to a tamper-evident log.
6. The service is provided as agreed in your contract. Replace this default text with your own legal terms (TERMS_FILE in the server settings).
"""


def text() -> str:
    path = getattr(settings, "TERMS_FILE", "")
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return DEFAULT
