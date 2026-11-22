#!/usr/bin/env python3
"""Own fixtures exercising the real prebuilt gettext/KI18n runtime."""

import subprocess

import polib
from i18n import PREFIX
from sdk import BUILD_DIR

output = BUILD_DIR / "i18n/fixtures"
output.mkdir(parents=True, exist_ok=True)
formats = {
    "zh_CN": "nplurals=1; plural=0;",
    "ru": "nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);",
    "ar": "nplurals=6; plural=(n==0 ? 0 : n==1 ? 1 : n==2 ? 2 : n%100>=3 && n%100<=10 ? 3 : n%100>=11 && n%100<=99 ? 4 : 5);",
    "de": "nplurals=2; plural=(n != 1);",
    "fr": "nplurals=2; plural=(n > 1);",
}


for language, plural in formats.items():
    catalog = polib.POFile()
    catalog.metadata = {
        "Language": language,
        "Content-Type": "text/plain; charset=UTF-8",
        "Plural-Forms": plural,
    }
    entries_by_language: dict[str, list[tuple[str, ...]]] = {
        "zh_CN": [
            ("Probe", "探针"),
            ("Open", "打开", "verb"),
            ("Open", "开放", "adjective"),
            ("Script %1", "fallback:%1|/|$[decorate %1]"),
        ],
        "ru": [("Probe", "Проба")],
        "de": [("Probe", "Probe-DE")],
        "fr": [("French only", "Seulement français")],
        "ar": [],
    }
    catalog.extend(
        polib.POEntry(
            msgid=entry[0],
            msgstr=entry[1],
            msgctxt=entry[2] if len(entry) == 3 else None,
        )
        for entry in entries_by_language[language]
    )
    forms = {
        "zh_CN": ["%1 项"],
        "ru": ["%1 one", "%1 few", "%1 many"],
        "ar": ["%1 zero", "%1 one", "%1 two", "%1 few", "%1 many", "%1 other"],
    }.get(language)
    if forms:
        catalog.append(
            polib.POEntry(
                msgid="%1 item",
                msgid_plural="%1 items",
                msgstr_plural=dict(enumerate(forms)),
            )
        )
    folder = output / "locale" / language
    (folder / "LC_MESSAGES").mkdir(parents=True, exist_ok=True)
    po = folder / "fixture.po"
    catalog.save(str(po))
    subprocess.run(
        [
            PREFIX / "host/bin/msgfmt",
            "--no-hash",
            "-o",
            folder / "LC_MESSAGES/sdk-i18n.mo",
            po,
        ],
        check=True,
    )
script = output / "locale/zh_CN/LC_SCRIPTS/sdk-i18n/sdk-i18n.js"
script.parent.mkdir(parents=True, exist_ok=True)
script.write_text(
    'Ts.setcall("decorate", function(value) { return "scripted:" + value; });\n'
)
print(output)
