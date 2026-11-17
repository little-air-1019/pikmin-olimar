"""把 helper 產生的 OCR JSON 去識別化（玩家名、地點、帳號一律換成 ＊），只留遊戲固定 UI 文字，供 tests/fixtures 使用。
用法：python scripts/anonymize_ocr.py runtime/evidence/xxx.ocr.json > tests/fixtures/xxx.ocr.json
"""
import json
import re
import sys

KEEP = ("蘑菇", "今天", "剩下", "參加", "参加", "選擇派出", "自動", "工作力", "取消", "前往這裡", "大家的合計", "特殊活動", "華麗",
        "香腸", "擅長", "查看全部", "一般", "探險", "皮克敏", "花苗", "明信片", "記錄", "券", "只有不怕毒")
d = json.load(open(sys.argv[1]))
for l in d["lines"]:
    t = l["text"]
    if not (any(k in t for k in KEEP) or re.fullmatch(r"[\d,./:\s（）()]*", t)):
        l["text"] = "＊"
print(json.dumps(d, ensure_ascii=False, indent=1))
