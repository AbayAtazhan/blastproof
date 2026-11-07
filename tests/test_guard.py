from app import guard


def rules(code):
    return {f.rule for f in guard.review(code)}


def test_clean_analysis_code_is_low_risk():
    code = "import pandas as pd\ndf = pd.read_csv('/data/x.csv')\nprint(df.sum())\n"
    assert guard.review(code) == []
    assert guard.risk_level(guard.review(code)) == "low"


def test_network_import_is_flagged_high():
    findings = guard.review("import urllib.request\n")
    assert findings[0].rule == "network" and findings[0].severity == "high"


def test_write_outside_out_is_flagged_but_out_is_fine():
    assert "write" in rules("open('/etc/hosts', 'w')")
    assert "write" not in rules("open('/out/table.csv', 'w')")


def test_env_access_and_unbounded_loop():
    assert "env" in rules("import os\nprint(os.environ)")
    assert "loop" in rules("while True:\n    pass")
    assert "loop" not in rules("while True:\n    break")


def test_syntax_error_is_reported_not_raised():
    assert guard.review("def (")[0].rule == "syntax"
