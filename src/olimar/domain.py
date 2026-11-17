"""蘑菇規則。純資料／純函式，不依賴 GUI。"""
from __future__ import annotations

from dataclasses import dataclass, field

MIN_TEAM = {"小型": 1, "一般": 10, "大型": 15, "巨大": 20}
MAX_PLAYERS = 5

# 只接受「完全符合」的種類名稱；未知（含活動蘑菇如「華麗」）一律交給遊戲自動選擇。
KIND_COLOR = {
    "毒": "白", "火": "紅", "水": "藍", "水晶": "岩", "電": "黃",
    "紅": "紅", "黃": "黃", "藍": "藍", "紫": "紫", "白": "白",
    "灰": "岩", "粉紅": "羽翅", "冰藍": "冰",
}


def color_for(kind: str | None) -> str | None:
    if not kind:
        return None
    k = kind.strip()
    for suf in ("色", "系", "屬性"):
        if k.endswith(suf):
            k = k[: -len(suf)]
    return KIND_COLOR.get(k)


@dataclass
class Mushroom:
    size: str | None
    kind: str | None
    title: str = ""

    @property
    def min_team(self) -> int | None:
        return MIN_TEAM.get(self.size or "")

    @property
    def color(self) -> str | None:
        return color_for(self.kind)


@dataclass
class Decision:
    action: str  # join | skip | stop
    reasons: list[str] = field(default_factory=list)
    mushroom: Mushroom | None = None

    def why(self) -> str:
        return "；".join(self.reasons)


def decide_detail(m: Mushroom, *, remaining: int | None, ticket: str, players_seen: int) -> Decision:
    """詳細頁能否加入。ticket: free|ticket|unknown。所有未知一律不加入。"""
    if remaining is None:
        return Decision("stop", ["未知今日剩餘免費次數（請先在探險清單頁讀取）"], m)
    if remaining <= 0:
        return Decision("stop", ["今日免費次數為 0"], m)
    if m.min_team is None:
        return Decision("skip", [f"未辨識蘑菇大小（{m.title!r}）"], m)
    if players_seen >= MAX_PLAYERS:
        return Decision("skip", [f"畫面可見 {players_seen} 位玩家（已滿 5 人）"], m)
    if ticket == "ticket":
        return Decision("skip", ["參加鈕旁有蘑菇券圖示（已滿或超過免費額度），不使用蘑菇券"], m)
    if ticket != "free":
        return Decision("skip", ["無法確定參加鈕是否需要蘑菇券，保守跳過"], m)
    why = [f"{m.size}蘑菇，最小隊伍 {m.min_team}", "參加鈕無券圖示", f"今日剩餘 {remaining} 次"]
    why.append(f"種類 {m.kind!r} → 建議顏色 {m.color}" if m.color else f"種類 {m.kind!r} 無顏色規則 → 交由遊戲自動")
    return Decision("join", why, m)
