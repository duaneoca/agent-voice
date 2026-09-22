"""Turning an agent's reply into something a voice can read.

Every case here is one that only shows up when you listen to the output.
"""
from speech_text import iter_sentences, is_stop_command, make_speakable


class TestSentenceChunking:
    def test_does_not_split_a_decimal(self):
        # "It is 2.47 PM" read as two sentences puts a full stop mid-number.
        assert list(iter_sentences(["The build took 2.47 seconds to finish."])) \
            == ["The build took 2.47 seconds to finish."]

    def test_does_not_split_an_initial(self):
        assert list(iter_sentences(["A commit from J. Smith landed on main."])) \
            == ["A commit from J. Smith landed on main."]

    def test_merges_fragments_too_short_to_speak(self):
        # "No." alone is a jarring thing to hear on its own.
        assert list(iter_sentences(["No. ", "It did not load."])) \
            == ["No. It did not load."]

    def test_splits_where_it_should(self):
        out = list(iter_sentences(["The first sentence is here. ",
                                   "The second sentence follows it."]))
        assert len(out) == 2

    def test_yields_across_delta_boundaries(self):
        # Tokens arrive mid-word; a boundary must still be found.
        out = list(iter_sentences(["The answer is fo", "rty two. ",
                                   "Nothing else to report here."]))
        assert out[0] == "The answer is forty two."

    def test_trailing_text_without_punctuation_is_not_lost(self):
        # A reply cut off mid-thought still has to be spoken.
        out = list(iter_sentences(["A sentence long enough to stand alone. ",
                                   "an unfinished tail"]))
        assert out == ["A sentence long enough to stand alone.", "an unfinished tail"]

    def test_a_short_lead_merges_into_the_tail(self):
        # "A complete one." is under the merge threshold, so it joins what
        # follows rather than being spoken as its own clipped utterance.
        assert list(iter_sentences(["A complete one. ", "an unfinished tail"])) \
            == ["A complete one. an unfinished tail"]


class TestMakeSpeakable:
    def test_strips_emphasis(self):
        assert "*" not in make_speakable("**bold** and *italic*")

    def test_code_fence_becomes_words(self):
        # Silence would lose the fact that code was offered at all.
        assert "Code omitted" in make_speakable("here:\n```sh\nrm -rf /\n```")

    def test_list_items_get_terminal_punctuation(self):
        # Without it the items run together into one long breath.
        assert make_speakable("- one\n- two") == "one. two."

    def test_link_keeps_its_text_not_its_url(self):
        out = make_speakable("see [the docs](https://example.com/x)")
        assert "the docs" in out and "example.com" not in out

    def test_table_pipes_do_not_survive(self):
        assert "|" not in make_speakable("| a | b |")


class TestStopCommand:
    def test_recognises_the_short_forms(self):
        for phrase in ("stop", "cancel that", "never mind", "be quiet"):
            assert is_stop_command(phrase), phrase

    def test_ignores_case_and_punctuation(self):
        assert is_stop_command("Stop!")

    def test_a_sentence_merely_containing_stop_is_not_one(self):
        # The whole point of matching the entire transcript.
        assert not is_stop_command("do not stop the deployment")
        assert not is_stop_command("stop the service please")
