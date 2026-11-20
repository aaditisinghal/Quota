import pytest

from quota.agent.answer import is_acknowledgment


@pytest.mark.parametrize("text", ["ok", "Thanks!", "thanks so much!!", "Got it, thanks.", "OK thanks", "👍 ok"])
def test_bare_acknowledgments_end_the_exchange(text):
    assert is_acknowledgment(text)


@pytest.mark.parametrize("text", ["hi", "Sounds good, let's discuss.", "Hmm, maybe.", "why is CL-0003 negative?",
                                  "Thanks, but why is Acme $12,000?", "ok approve A-0002"])
def test_anything_more_still_gets_read(text):
    assert not is_acknowledgment(text)
