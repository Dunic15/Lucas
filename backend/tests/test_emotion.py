"""Emotion classifier: the face's mood comes from here, so its defaults and
precedence are load-bearing (a wrong 'happy' on bad news is the visible bug)."""
from app import emotion


def test_neutral_is_the_floor():
    assert emotion.classify("The deployment is scheduled for Tuesday.") == "neutral"
    assert emotion.classify("") == "neutral"
    assert emotion.classify("Who owns the migration step?") == "neutral"


def test_bad_news_reads_concerned_bilingual():
    assert emotion.classify("Unfortunately we're behind schedule.") == "concerned"
    assert emotion.classify("C'è un problema con l'approvazione.") == "concerned"
    assert emotion.classify("That milestone is at risk.") == "concerned"


def test_praise_reads_happy_and_excited():
    assert emotion.classify("Great job closing that early.") == "happy"
    assert emotion.classify("Complimenti a tutti!") == "excited"  # praise + "!"
    assert emotion.classify("Perfect.") == "happy"


def test_concern_outranks_praise():
    # A line that praises AND flags a risk must not read as celebratory.
    assert emotion.classify("Great work, but the security approval is missing.") == "concerned"


def test_serious_for_stakes_words():
    assert emotion.classify("This deadline is mandatory.") == "serious"
    assert emotion.classify("È un requisito di conformità.") == "serious"


def test_normalize_coerces_unknown_to_neutral():
    assert emotion.normalize("HAPPY") == "happy"
    assert emotion.normalize("ecstatic") == "neutral"  # not in the set
    assert emotion.normalize(None) == "neutral"
    assert emotion.normalize("") == "neutral"


def test_face_mappings_are_valid():
    # Every mood maps to a real TalkingHead mood and a valid Ditto emo (0-7).
    for label in ("neutral", "happy", "excited", "serious", "concerned", "bogus"):
        assert emotion.talk_mood(label) in {"neutral", "happy", "sad", "angry", "love"}
        assert 0 <= emotion.ditto_emo(label) <= 7


def test_default_is_neutral_everywhere():
    assert emotion.talk_mood(None) == "neutral"
    assert emotion.ditto_emo(None) == 4  # Ditto's documented neutral default


def test_ditto_indices_match_ditto_emotion_order():
    # Ditto's order (its condition_handler.py): 0 Angry, 1 Disgust, 2 Fear,
    # 3 Happy, 4 Neutral, 5 Sad, 6 Surprise, 7 Contempt. A wrong index here
    # means a wrong FACE (happy praise rendered as disgust) — pin them.
    assert emotion.ditto_emo("happy") == 3
    assert emotion.ditto_emo("excited") == 3
    assert emotion.ditto_emo("concerned") == 5
    assert emotion.ditto_emo("serious") == 4
    # never map to the hostile emotions
    assert all(
        emotion.ditto_emo(m) not in (0, 1, 2, 7)
        for m in ("neutral", "happy", "excited", "serious", "concerned")
    )
