"""Rank complete spoken moments with source-relative, speech-safe boundaries."""

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from statistics import median

from .speech import ENERGY_STEP


TERMINAL = re.compile(r"[.!?。！？।؟][\"'”’»)]*$")
QUESTION = re.compile(r"[?？؟][\"'”’»)]*$")
QUESTION_OPEN = re.compile(r"^(?:why|how|what|where|when|who)\s+(?:do|does|did|is|are|was|were|would|could|should|can|will|happened)\b", re.I)
CUE_CONTINUATION = re.compile(r"^(?:a|an|the|to|with|of|in|for|by|my|our|your|and|or|but|because|who|which|whose|that)$", re.I)
CLAUSE_START = re.compile(r"^(?:then|when|so|anyway|instead|but|finally|because|actually|while|however|now|we|you|I|they|he|she|it)$", re.I)
TOKEN = re.compile(r"[\u3400-\u9fff\u3040-\u30ff]|[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
INTRO = re.compile(r"\b(welcome back|welcome to (?:the|my)|thanks? for (?:watching|joining)|like and subscribe|sponsor(?:ed by)?)\b", re.I)
CONTINUATION = re.compile(r"^(?:and|but|then|because|so|it|that|this|they|therefore|which|also)\b", re.I)
PROMISE = re.compile(r"\b(turns? out|here(?:'s| is) (?:why|how|what)|the trick|the secret|nobody|never expected|imagine|what if|surpris\w*|I thought|I used to)\b", re.I)
PAYOFF = re.compile(r"\b(that(?:'s| is) why|in the end|the result|we (?:learned|realized|found)|I (?:learned|realized)|turn(?:ed|s) out|afterward|instead|finally|because|which meant)\b", re.I)
REACTION = re.compile(r"[\[(](?:laugh(?:s|ing|ter)?|applause|cheers?|gasp(?:s)?)[\])]", re.I)
ABBREVIATION = re.compile(r"(?:\b(?:Mr|Mrs|Ms|Dr|St|vs|e\.g|i\.e)|\b[A-Z])\.$")
STOP = set("a about after all also am an and are as at back be been before but by can could did do does for from get got had has have how i if in is it its just know let like me more most my no not now of okay on one only or our out people really right said say she so some than that the their them then there these they thing this those through to too uh um up us use very want was we well were what when where which who why will with would yeah yes you your".split())
PROFILES = {
    "podcast": "conversation experience perspective disagreement relationship argument opinion personal",
    "education": "explain measure example experiment learn steps practice evidence means compare",
    "comedy": "joke funny laughed laughter absurd ridiculous hilarious unexpected punchline",
    "motivation": "practice change goal overcome courage effort work success habit fear",
    "philosophy": "meaning freedom moral ethics truth belief mind justice knowledge identity",
    "gaming": "game round player team opponent match win score level boss run",
    "story": "remember happened story suddenly moment time when walked arrived discovered",
}


@dataclass
class Sentence:
    first: int
    last: int
    start: float
    end: float
    text: str
    terms: Counter
    terminal: bool


def _joined(words):
    text = " ".join(word["text"].strip() for word in words)
    text = re.sub(r"\s+([?!.,;:…%)\]。！？、।؟])", r"\1", text)
    return re.sub(r"(?<=[\u3040-\u30ff\u3400-\u9fff]) (?=[\u3040-\u30ff\u3400-\u9fff])", "", text)


def clip_context(words, start, end):
    """Return source words audible in a manually selected range."""
    return _joined(word for word in words if word["end"] > start + 1e-6 and word["start"] < end - 1e-6)


def _terms(text):
    return Counter(term for term in TOKEN.findall(text.casefold()) if term not in STOP)


def _sentences(words):
    sentences = []
    first = 0
    for index, word in enumerate(words):
        following = words[index + 1] if index + 1 < len(words) else None
        gap = following["start"] - word["end"] if following else math.inf
        terminal = bool(TERMINAL.search(word["text"]) and not ABBREVIATION.search(word["text"]))
        cue_break = (word.get("cue_end") and index - first >= 4 and not CUE_CONTINUATION.match(word["text"])
                     and (gap >= 0.14 or (following and CLAUSE_START.match(following["text"]))))
        # Speech recognition can separate a question and its answer with a comma.
        question_break = (
            following and 4 <= index - first <= 24 and word["text"].endswith(",")
            and not CUE_CONTINUATION.match(word["text"].rstrip(","))
            and QUESTION_OPEN.match(_joined(words[first:first + 3]))
        )
        if terminal or gap >= 0.65 or cue_break or question_break:
            text = _joined(words[first:index + 1])
            sentences.append(Sentence(first, index, words[first]["start"], word["end"], text, _terms(text), terminal))
            first = index + 1
    return sentences


def _cosine(left, right):
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    dot = sum(value * right.get(key, 0) for key, value in left.items())
    if not dot:
        return 0.0
    scale = math.sqrt(sum(v * v for v in left.values()) * sum(v * v for v in right.values()))
    return dot / scale


def _context_model(sentences):
    """Fit sparse TF–IDF and a small co-occurrence neighborhood on this source."""
    document_count = Counter()
    for sentence in sentences:
        document_count.update(sentence.terms.keys())
    idf = {term: 1 + math.log((len(sentences) + 1) / (count + 1)) for term, count in document_count.items()}
    shared = defaultdict(Counter)
    for sentence in sentences:
        # Cap contribution from an unpunctuated lecture or a caption dump.
        terms = list(sentence.terms)[:18]
        for index, term in enumerate(terms):
            if document_count[term] < 2 or document_count[term] > max(3, len(sentences) / 4):
                continue
            neighbors = terms[max(0, index - 4):index] + terms[index + 1:index + 5]
            shared[term].update(neighbor for neighbor in neighbors if document_count[neighbor] >= 2)
    links = {term: [neighbor for neighbor, count in neighbors.most_common(4) if count >= 2]
             for term, neighbors in shared.items()}
    return idf, links


def _vector(terms, idf, links):
    vector = Counter()
    for term, frequency in terms.items():
        weight = (1 + math.log(frequency)) * idf.get(term, 1.0)
        vector[term] += weight
        for neighbor in links.get(term, ()):
            vector[neighbor] += weight * 0.15
    return vector


def _bounds(first, last, words, duration, minimum, maximum, target):
    before = words[first.first - 1]["end"] if first.first else 0
    after = words[last.last + 1]["start"] if last.last + 1 < len(words) else duration
    pad_left = min(1, max(0, first.start - before - 0.015))
    pad_right = min(1, max(0, after - last.end - 0.015))
    length = last.end - first.start
    available = min(maximum, length + pad_left + pad_right)
    wanted = target if target is not None else max(minimum, min(available, length + 0.24))
    if wanted < length - 1e-8 or wanted > available + 1e-8:
        return None
    extra = max(0, wanted - length)
    left = min(pad_left, max(extra - pad_right, min(0.12, extra / 2)))
    start = round(first.start - left, 4)
    end = round(start + wanted, 4)
    if end > duration + 0.00005 or not minimum - 0.0001 <= end - start <= maximum + 0.0001:
        return None
    return start, min(end, duration)


def _category(segment, profiles, requested, question, reaction, payoff):
    profile_scores = {name: _cosine(segment, profile) for name, profile in profiles.items()}
    profile_scores["education"] += 0.08 * bool(question)
    profile_scores["comedy"] += 0.25 * bool(reaction)
    profile_scores["story"] += 0.08 * bool(payoff)
    best = max(profile_scores, key=profile_scores.get)
    category = best if profile_scores[best] >= 0.10 else (requested if requested in PROFILES else "podcast")
    relevance = profile_scores.get(requested, profile_scores[best])
    return category, min(1.0, 3 * relevance)


def _candidate(first, last, terms, sentences, index, stop, limits, model, audio):
    (words, duration, minimum, maximum, target, requested) = limits
    idf, links, vectors, profiles, adjacent_similarity = model
    energy, typical_energy = audio
    spoken = last.last - first.first + 1
    length = last.end - first.start
    if spoken < 9 or len(terms) < 6 or _question(last.text):
        return None
    bounds = _bounds(first, last, words, duration, minimum, maximum, target)
    if not bounds or INTRO.search(first.text):
        return None
    start, end = bounds
    question = _question(first.text)
    promise = bool(PROMISE.search(first.text))
    payoff = bool(PAYOFF.search(last.text))
    text = " ".join(sentence.text for sentence in sentences[index:stop + 1])
    reaction = bool(REACTION.search(text))
    coherence = 0.0
    if stop > index:
        thread_similarity = adjacent_similarity[index:stop]
        coherence = max(_cosine(vectors[index], vectors[stop]), sum(thread_similarity) / len(thread_similarity))
        immediate_response = sentences[index + 1].start - first.end < 0.35
        if coherence < 0.035 and not (immediate_response and (question or payoff or reaction)):
            return None
    segment = _vector(terms, idf, links)
    # Prefer a topic turn that can stand alone. High similarity to surrounding
    # material plus a backward reference often means a dependent opening.
    preceding_gap = first.start - sentences[index - 1].end if index else math.inf
    dependency = bool(CONTINUATION.search(first.text)) and preceding_gap < 1.0
    before_similarity = _cosine(vectors[index - 1], segment) if preceding_gap < 1.0 else 0.0
    gap = sentences[stop + 1].start - last.end if stop + 1 < len(sentences) else duration - last.end
    ending = (0.60 if last.terminal else 0.30) + 0.25 * min(1, max(0, gap) / .8) + 0.15 * payoff
    hook = (0.69 if question or promise else 0.24) + 0.16 * (not dependency) + 0.15 * (1 - before_similarity)
    coverage = sum(word["end"] - word["start"] for word in words[first.first:last.last + 1]) / length
    density = min(1, spoken / length / 1.6) * min(1, coverage / 0.65)
    variety = min(1, len(terms) / max(8, spoken * 0.48))
    audio_score = 0.0
    if energy and typical_energy is not None:
        bins = energy[int(first.start / ENERGY_STEP):math.ceil(last.end / ENERGY_STEP)]
        if bins:
            audio_score = min(1, max(0, max(bins) - typical_energy) / 13)
    category, relevance = _category(segment, profiles, requested, question, reaction, payoff)
    thread_score = min(1, coherence * 2.7 + 0.42 * question + 0.42 * payoff + 0.2 * reaction)
    score = (24 * hook + 21 * ending + 25 * thread_score + 11 * density
             + 10 * variety + 5 * relevance + 4 * audio_score - 12 * dependency)
    if score < 49 or thread_score < 0.20:
        return None
    signals = []
    if question:
        signals.append("Opens with a question and includes the ensuing response")
    elif promise:
        signals.append("Opens with a promise or contrast")
    elif coherence >= 0.08:
        signals.append("Carries related ideas from the opening into the ending")
    if payoff:
        signals.append("includes a stated outcome or explanation")
    if reaction:
        signals.append("includes a transcript-marked audience reaction")
    if ending >= 0.7:
        signals.append("ends at a speech boundary")
    if audio_score >= 0.8:
        signals.append("contains an above-baseline audio peak")
    reason = ". ".join(signal[0].upper() + signal[1:] for signal in signals) or "Clear pacing and a coherent closing point"
    return (dict(start=start, end=end, duration=round(end - start, 4), text=text,
                 hook=first.text[:220], reason=reason + ".",
                 score=round(score, 1), category=category,
                 signals={"hook": round(hook, 2), "ending": round(ending, 2),
                          "context": round(thread_score, 2), "audio_peak": round(audio_score, 2)}), segment)


def _question(text):
    return bool(QUESTION.search(text) or (not TERMINAL.search(text) and QUESTION_OPEN.match(text)))


def select_highlights(words, duration, settings, energy=None):
    """Search event-shaped speech spans throughout a video and rank reviewable clips."""
    words = sorted((dict(text=w["text"].strip(), start=max(0, w["start"]), end=min(duration, w["end"]), cue_end=w.get("cue_end", False))
                    for w in words if w["text"].strip() and math.isfinite(w["start"]) and math.isfinite(w["end"])),
                   key=lambda word: (word["start"], word["end"]))
    words = [word for word in words if word["end"] > word["start"]]
    if not words or settings["count"] < 1:
        return []
    minimum, maximum = settings["min_duration"], settings["max_duration"]
    target = settings.get("target_duration")
    requested = settings.get("category", "auto")
    sentences = _sentences(words)
    idf, links = _context_model(sentences)
    vectors = [_vector(sentence.terms, idf, links) for sentence in sentences]
    profiles = {name: _vector(_terms(profile), idf, links) for name, profile in PROFILES.items()}
    adjacent_similarity = [_cosine(left, right) for left, right in zip(vectors, vectors[1:])]
    audio = energy or []
    audible = [value for value in audio if value > -55]
    limits = (words, duration, minimum, maximum, target, requested)
    model = (idf, links, vectors, profiles, adjacent_similarity)
    audio = (audio, median(audible) if audible else None)
    candidates = []
    for index, first in enumerate(sentences):
        terms = Counter()
        best = None
        for stop in range(index, len(sentences)):
            last = sentences[stop]
            length = last.end - first.start
            if length > (target if target is not None else maximum):
                break
            if stop > index and last.start - sentences[stop - 1].end > 3.0:
                break
            terms.update(last.terms)
            if length + 2 < (target if target is not None else minimum):
                continue
            candidate = _candidate(first, last, terms, sentences, index, stop, limits, model, audio)
            if candidate and (best is None or candidate[0]["score"] > best[0]["score"]):
                best = candidate
        if best:
            candidates.append(best)
    selected = []
    for clip, terms in sorted(candidates, key=lambda item: (-item[0]["score"], item[0]["start"])):
        if any((clip["start"] < other["end"] and other["start"] < clip["end"]) or _cosine(terms, other_terms) > 0.86
               for other, other_terms in selected):
            continue
        selected.append((clip, terms))
        if len(selected) >= settings["count"]:
            break
    return [clip for clip, _ in selected]
