import pytest

from resumebot.engine import questions
from resumebot.engine.questions import Answerer, Field, NeedsHuman, closest_option, from_memory, from_rules


@pytest.fixture(autouse=True)
def patch_answers(monkeypatch):
    from resumebot import config
    monkeypatch.setattr(questions, "answers", config.answers)


@pytest.mark.parametrize("label,expected", [
    ("First Name *", "Ada"),
    ("Last name", "Lovelace"),
    ("Email address", "ada@example.com"),
    ("Mobile phone number", "+1 416 555 0100"),
    ("LinkedIn Profile", "linkedin.com/in/ada"),
    ("Will you now or in the future require sponsorship?", "No"),
    ("Are you legally authorized to work in Canada?", "Yes"),
    ("Are you legally authorized to work in the United States?", "No"),
    ("Desired salary", "120000"),
])
def test_rules(label, expected):
    assert from_rules(Field(label)) == expected


def test_us_job_location_switches_answers():
    assert from_rules(Field("Will you require visa sponsorship?"), job_location="New York, NY, United States") == "Yes"
    assert from_rules(Field("Expected salary"), job_location="Remote - USA") == "95000"


def test_options_are_matched():
    f = Field("Do you require sponsorship?", "radio", ["Yes", "No"])
    assert from_rules(f) == "No"
    assert closest_option("Decline to answer", ["Male", "Female", "I don't wish to answer"]) == "I don't wish to answer"


def test_custom_answers_fuzzy():
    assert from_memory(Field("How did you hear about us")) == "Job board"


def test_learned_answers_roundtrip():
    questions.remember("Do you have a valid G driver's licence?", "Yes")
    assert from_memory(Field("Do you have a valid G drivers licence?")) == "Yes"


async def test_unknown_required_question_goes_to_human(monkeypatch):
    async def fake_llm(f, *a):
        return "Yes", False, 0.3  # model isn't grounded → must not guess
    monkeypatch.setattr(questions, "from_llm", fake_llm)
    ans = Answerer("Engineer", "Acme", "desc")
    with pytest.raises(NeedsHuman):
        await ans(Field("Do you hold an active Secret clearance?", "radio", ["Yes", "No"], required=True))
    assert await ans(Field("Anything else to add?", "textarea", None, required=False)) == ""


async def test_custom_answer_beats_generic_rule(monkeypatch):
    from resumebot import config
    base = config.answers()
    monkeypatch.setattr(questions, "answers", lambda: {**base, "custom": {
        "What type of visa sponsorship will you require?": "TN status under USMCA"}})
    ans = Answerer("Engineer", "Acme", "desc", "Remote - USA")
    assert await ans(Field("What type of visa sponsorship will you require?", "textarea")) == "TN status under USMCA"
    assert await ans(Field("Will you require visa sponsorship?", "radio", ["Yes", "No"])) == "Yes"


def test_ashby_location_and_sponsorship_rules():
    assert from_rules(Field('Location')) == questions._a('contact.city')
    assert from_rules(Field('Will you require sponsorship?'), 'San Francisco, California') == 'Yes'
    assert from_rules(Field('Are you legally authorized to work in Canada?'), 'San Francisco, California') == 'Yes'


def test_work_arrangement_and_eeo_rules_need_the_real_question(monkeypatch):
    from resumebot import config
    base = config.answers()
    monkeypatch.setattr(questions, "answers", lambda: {
        **base, "logistics": {**base["logistics"], "open_to_remote": "Yes", "open_to_hybrid": "Yes",
                              "open_to_onsite": "Yes"},
        "eeo": {**base["eeo"], "race_ethnicity": "Decline to answer"}})
    assert from_rules(Field("Are you open to remote work?")) == "Yes"
    assert from_rules(Field("Are you willing to work on-site in Toronto?")) == "Yes"
    assert from_rules(Field("Race / Ethnicity")) == "Decline to answer"
    for label in ("Experience with remote access tools?", "Describe your experience with packet tracing",
                  "Have you worked with hybrid cloud environments?", "Years of experience in Office 365"):
        assert from_rules(Field(label)) is None, label


@pytest.mark.parametrize("label,expected", [
    ("Location (City)*", "Toronto"), ("Current location", "Toronto"), ("What city do you live in?", "Toronto"),
    ("Location (city, province)", "Toronto, ON"), ("City and state", "Toronto, ON"), ("Province", "ON"),
])
def test_location_question_variants(label, expected):
    assert from_rules(Field(label)) == expected


def test_answer_matches_the_right_city_option():
    opts = ["Toronto, Ohio, United States", "Toronto, Ontario, Canada", "Vancouver, British Columbia, Canada"]
    assert closest_option("Toronto", opts) == "Toronto, Ontario, Canada"
    assert from_rules(Field("Location (City)*", "select", opts)) == "Toronto, Ontario, Canada"
    assert closest_option("No", ["Yes", "No"]) == "No"
    assert closest_option("Paris", opts) is None


@pytest.mark.parametrize("label", ["Where are you currently located?", "Where are you based?",
                                   "Where are you located?"])
def test_where_located_questions(label):
    assert from_rules(Field(label)) == "Toronto, ON"
