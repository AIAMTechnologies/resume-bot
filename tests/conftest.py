import os
import tempfile

# Isolated data dir + DB for every test run; must be set before resumebot imports.
os.environ["RESUMEBOT_DATA_DIR"] = tempfile.mkdtemp(prefix="resumebot-test-")

import pytest  # noqa: E402

from resumebot import config, db  # noqa: E402

db.init_db()


@pytest.fixture(autouse=True)
def fresh_config(monkeypatch):
    config.reload()
    monkeypatch.setattr(config, "answers", lambda: {
        "contact": {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com",
                    "phone": "+1 416 555 0100", "city": "Toronto", "province_state": "ON",
                    "linkedin": "linkedin.com/in/ada", "github": "github.com/ada"},
        "work_authorization": {"canada": "Yes", "united_states": "No", "requires_sponsorship_canada": "No",
                               "requires_sponsorship_us": "Yes", "willing_to_relocate": "Yes"},
        "logistics": {"desired_salary_cad": "120000", "desired_salary_usd": "95000", "notice_period": "2 weeks"},
        "eeo": {"gender": "Decline to answer"},
        "custom": {"How did you hear about us?": "Job board"},
    })
    yield
