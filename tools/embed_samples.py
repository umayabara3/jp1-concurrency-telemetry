"""jp1-telemetry.html にサンプル CSV を埋め込む（単一ファイルで「サンプル」ボタンが動くように）。

<script type="text/plain" id="..." data-src="samples/xxx.csv"> ... </script> の中身を
data-src のファイル内容で置き換える。samples/ を作り直したら実行する。

    python tools/embed_samples.py          # 埋め込みを更新
    python tools/embed_samples.py --check  # 埋め込みが samples/ と一致するか検査（不一致で終了コード 1）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "jp1-telemetry.html"
TAG = re.compile(r'(<script type="text/plain" id="[^"]+" data-src="([^"]+)">)(.*?)(</script>)', re.S)


def render(html: str) -> str:
    def sub(m: re.Match) -> str:
        body = (ROOT / m.group(2)).read_text(encoding="utf-8")
        if "</script" in body.lower():
            raise SystemExit(f"{m.group(2)} に </script が含まれるため埋め込めません")
        return m.group(1) + "\n" + body.strip() + "\n" + m.group(4)
    return TAG.sub(sub, html)


def main() -> int:
    current = HTML.read_text(encoding="utf-8")
    updated = render(current)
    if "--check" in sys.argv:
        if current != updated:
            print("埋め込みサンプルが samples/ と一致しません。python tools/embed_samples.py を実行してください。")
            return 1
        print("embedded samples: OK")
        return 0
    HTML.write_text(updated, encoding="utf-8", newline="\n")
    print(f"embedded {len(TAG.findall(updated))} samples into {HTML.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
