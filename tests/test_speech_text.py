"""Turning an agent's reply into something a voice can read.

Every case here is one that only shows up when you listen to the output.
"""
from speech_text import (
    as_prompt, iter_sentences, is_stop_command, make_speakable,
)


class TestSentenceChunking:
    def test_does_not_split_a_decimal(self):
        # "It is 2.47 PM" read as two sentences puts a full stop mid-number.
        assert list(iter_sentences(["The build took 2.47 seconds to finish."])) \
            == ["The build took 2.47 seconds to finish."]

    def test_does_not_split_an_initial(self):
        assert list(iter_sentences(["A commit from J. Smith landed on main."])) \
            == ["A commit from J. Smith landed on main."]

    def test_a_sentence_ending_in_a_year_still_ends(self):
        """The old guard refused a boundary after any digit, so this was one
        utterance -- and so was everything that followed it."""
        out = list(iter_sentences(["It shipped in 2024 at last. ",
                                   "The rewrite came later on."]))
        assert len(out) == 2, out

    def test_a_sentence_ending_in_an_acronym_still_ends(self):
        """Same guard, the other half: any capital before the stop blocked
        it, so "fix the GPU." never ended a sentence."""
        out = list(iter_sentences(["You should go and fix the GPU. ",
                                   "Then reboot the machine."]))
        assert len(out) == 2, out

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


class TestCodeIsNotReadAloud:
    """The split happens before the flattening, which is the whole problem.

    `make_speakable` turns a *complete* fenced block into "Code omitted". It
    is handed one sentence at a time, so a block containing ". " was cut up
    first: the opening piece became "Code omitted" and every piece after it
    was spoken as code, one line at a time.
    """

    def test_a_fenced_block_containing_sentences_is_not_split(self):
        reply = ("Here is the fix. ```python\nx = 1. y = 2. print(x)\n``` "
                 "That is all. Done now.")
        chunks = list(iter_sentences([reply]))
        spoken = [make_speakable(c) for c in chunks]
        assert all("x = 1" not in s for s in spoken), spoken
        assert any("Code omitted" in s for s in spoken), spoken
        assert "Done now." in " ".join(spoken)

    def test_it_splits_normally_around_a_closed_fence(self):
        reply = "First. ```\ncode\n``` Second sentence here. Third one here."
        chunks = list(iter_sentences([reply]))
        assert len(chunks) > 1, chunks

    def test_an_unclosed_fence_still_reaches_the_end(self):
        """An agent cut off mid-block must not leave the answer unspoken."""
        reply = "Here. ```python\nx = 1. y = 2."
        spoken = [make_speakable(c) for c in iter_sentences([reply])]
        assert spoken and any(s for s in spoken), spoken
        assert all("y = 2" not in s for s in spoken), spoken

    def test_a_fence_arriving_a_delta_at_a_time_behaves_the_same(self):
        deltas = ["Here is the fix. ``", "`python\nx = 1. ", "y = 2.\n``", "` Done now."]
        spoken = [make_speakable(c) for c in iter_sentences(deltas)]
        assert all("x = 1" not in s for s in spoken), spoken


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


class TestAsPrompt:
    """A transcript becomes an argv element, so a leading hyphen is a flag.

    Whisper emits one for a pause, a dictated dash or a false start. The
    worst case is an unknown-flag error rather than something running, but
    it is a turn lost to a character nobody said.
    """

    def test_a_leading_hyphen_comes_off(self):
        assert as_prompt("- what time is it") == "what time is it"
        assert as_prompt("-- list the files") == "list the files"

    def test_a_typographic_dash_comes_off_too(self):
        assert as_prompt("— run the tests") == "run the tests"

    def test_a_hyphen_inside_the_sentence_is_left_alone(self):
        assert as_prompt("check the agent-voice repo") == "check the agent-voice repo"

    def test_control_characters_come_off(self):
        assert as_prompt("hello\x1b]52;c;x\x07 there") == "hello]52;c;x there"

    def test_ordinary_speech_is_untouched(self):
        assert as_prompt("  what is the weather  ") == "what is the weather"

    def test_nothing_but_dashes_is_nothing(self):
        assert as_prompt("---") == ""


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
