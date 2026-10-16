from signals import engine


class FakeSafety:
    def __init__(self, penalty, after_assess=None):
        self.penalty, self.after, self.assessed = penalty, after_assess, []

    def signal_penalty(self, conn, mint):
        return self.penalty

    def assess(self, mint, conn, deep=True):
        self.assessed.append((mint, deep))
        if self.after:
            self.penalty = self.after


def base():
    return {"score": 80.0, "contributions": {"inflow": {"points": 80.0}}}


def test_penalty_scales_score_and_explains(monkeypatch):
    monkeypatch.setattr(engine, "_safety", FakeSafety(
        {"factor": 0.5, "risk": 50, "level": "high", "checked": True, "reasons": ["Top-10 halten 70 %"]}))
    m = engine.apply_safety(None, "MINT", base())
    assert m["score"] == 40.0 and m["score_raw"] == 80.0
    assert m["contributions"]["safety"]["points"] == -40.0
    assert "Top-10" in m["contributions"]["safety"]["why"]


def test_unchecked_and_low_risk_keep_score(monkeypatch):
    monkeypatch.setattr(engine, "_safety", FakeSafety({"factor": 1.0, "checked": False, "reasons": []}))
    m = engine.apply_safety(None, "MINT", base())
    assert m["score"] == 80.0 and "safety" not in m["contributions"]


def test_unchecked_coin_is_checked_before_it_fires(monkeypatch):
    fake = FakeSafety({"factor": 1.0, "checked": False, "reasons": []},
                      after_assess={"factor": 0.0, "risk": 95, "level": "critical", "blocked": True,
                                    "checked": True, "reasons": ["Mint-Authority aktiv"]})
    monkeypatch.setattr(engine, "_safety", fake)
    m = engine.check_before_signal(None, "MINT", engine.apply_safety(None, "MINT", base()))
    assert fake.assessed == [("MINT", False)] and m["score"] == 0.0


def test_no_safety_package(monkeypatch):
    monkeypatch.setattr(engine, "_safety", None)
    assert engine.apply_safety(None, "MINT", base())["score"] == 80.0
