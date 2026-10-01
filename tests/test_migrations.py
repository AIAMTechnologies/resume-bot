"""Existing databases gain new columns without a rebuild."""
from sqlalchemy import text

from resumebot import db
from resumebot.models import Job, LearnedAnswer


def test_missing_columns_are_added_with_defaults():
    with db.engine.begin() as conn:
        conn.execute(text('ALTER TABLE job DROP COLUMN score_attempts'))
        conn.execute(text('ALTER TABLE learnedanswer DROP COLUMN origin'))
    added = db.add_missing_columns()
    assert {'job.score_attempts', 'learnedanswer.origin'} <= set(added)
    job = db.save(Job(source='greenhouse', external_id='mig-1', company='C', title='T', url='u'))
    db.save(LearnedAnswer(question_norm='mig q', question='Mig q?', answer='Yes'))
    with db.session() as s:
        assert s.get(Job, job.id).score_attempts == 0
        assert s.exec(db.select(LearnedAnswer).where(LearnedAnswer.question_norm == 'mig q')).one().origin == 'you'
    assert db.add_missing_columns() == []
