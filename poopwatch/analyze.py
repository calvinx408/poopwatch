"""The two classification stages: a cheap gate, then the real verdict.

Stage 2 (gate) answers one narrow question -- "is an animal in a defecation
posture here?" -- on a few frames. It runs on every camera trigger, so it must
be cheap: a local Ollama model by default.

Stage 3 (verdict) only runs on gate hits. It gets the full labeled frame
sequence and answers the question that actually matters: did a dog defecate,
was a handler with it, and did they pick it up.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal, Sequence

from pydantic import BaseModel, Field, field_validator

from .capture import Frame
from .config import Config, config
from .vision import ask_claude, ask_ollama

log = logging.getLogger(__name__)

NOT_OBSERVED = -1.0

Subject = Literal["dog", "human", "other_animal", "none"]


class _Scored(BaseModel):
    """Base for results carrying a confidence score.

    Small local models routinely answer "confidence: 100" meaning 100%, despite
    a 0-1 schema. Normalizing is much better than throwing the event away.
    """

    @field_validator("confidence", mode="before", check_fields=False)
    @classmethod
    def _normalize_confidence(cls, v: object) -> object:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            f = float(v)
            if f > 1.0:
                f = f / 100.0
            return min(max(f, 0.0), 1.0)
        return v


class GateResult(_Scored):
    """Cheap first-pass posture screen."""

    subject: Subject = Field(description="The main animal or person in frame")
    posture: Literal["squatting", "standing", "walking", "lying", "other"] = Field(
        description="Posture of the subject in the most distinctive frame"
    )
    likely_defecating: bool = Field(
        description="True if any frame plausibly shows defecation in progress"
    )
    confidence: float = Field(ge=0.0, le=1.0)


class Verdict(_Scored):
    """Full determination over the event window."""

    subject: Subject
    defecation_occurred: bool
    defecation_offset_s: float = Field(
        description="Clip offset in seconds when it happened, or -1 if not observed"
    )
    handler_present: bool = Field(
        description="True if a person appears to be with the animal"
    )
    cleanup_occurred: bool
    cleanup_offset_s: float = Field(
        description="Clip offset in seconds when cleanup happened, or -1"
    )
    cleanup_evidence: str = Field(
        description="What was actually seen, or why cleanup could not be confirmed"
    )
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(description="Two or three sentences of justification")

    @property
    def is_violation(self) -> bool:
        """A dog defecated and nobody picked it up. Computed here rather than
        asked of the model, so the alerting rule is deterministic."""
        return (
            self.subject == "dog"
            and self.defecation_occurred
            and not self.cleanup_occurred
        )


GATE_PROMPT = """You are screening frames from a fixed outdoor security camera.

Answer one question: does any frame plausibly show an animal or person in the
act of defecating?

Signs of a dog defecating:
- Hind legs bent, rear lowered, back hunched into a rounded arch
- Tail raised or held stiffly out
- Stationary in one spot, often with a slight tremble or shuffle
- Frequently followed by scratching at the ground or sniffing the spot

Do NOT report defecation for: sniffing the ground with legs straight, lying
down, shaking, urinating with a raised leg, sitting upright, or simply walking.

Be permissive at this stage -- a later, more careful check will confirm. If the
posture is ambiguous but could be defecation, say true. If there is clearly no
animal, or the animal is plainly just walking or standing, say false.

Report confidence as a decimal between 0.0 and 1.0 (for example 0.8), not a
percentage."""


VERDICT_PROMPT = """You are reviewing a sequence of frames from a fixed outdoor
security camera. The frames are in chronological order and each is labeled with
its offset in seconds from the start of the clip.

{frame_index}

Determine, carefully:

1. SUBJECT -- Is the main actor a dog, a human, or another animal? The user
   cares primarily about dogs.

2. DEFECATION -- Did the subject actually defecate? Look for a dog with its
   rear lowered, hind legs bent, back arched, held stationary for several
   seconds across consecutive frames. A single ambiguous frame is not enough:
   require the posture to persist or to be followed by typical post-defecation
   behavior (scratching the ground, sniffing the spot, moving off). Note the
   offset of the clearest frame. If a human appears to defecate, report
   subject="human" and record it the same way.

3. HANDLER -- Is a person present who appears to be with the animal (holding a
   leash, walking alongside, waiting nearby)? A person elsewhere in frame who
   is unrelated does not count.

4. CLEANUP -- Did anyone pick up the waste? Positive evidence looks like: a
   person approaching the exact spot, bending or crouching over it, a hand or
   bag reaching toward the ground, then departing -- often carrying a small bag.
   Be strict here. Do NOT infer cleanup merely because the person walked near
   the spot, bent down for another reason, or the clip ended. If the clip ends
   before anyone returns, cleanup_occurred is false and you should say so in
   cleanup_evidence.

Be conservative overall: this triggers a real notification. If you cannot
clearly see defecation, say defecation_occurred=false and explain why in
reasoning. Low image quality, darkness, or the subject being out of frame are
all valid reasons to decline.

Report confidence as a decimal between 0.0 and 1.0 (for example 0.8), not a
percentage. Use -1 for any offset you did not observe."""


def _frame_index(frames: Sequence[Frame], trigger_offset: float) -> str:
    lines = ["Frames supplied, in order:"]
    for i, f in enumerate(frames, 1):
        lines.append(
            "  image %d: t=%.0fs (clock %s)" % (i, f.offset_s, f.clock)
        )
    lines.append(
        "\nThe camera's own motion/animal detector fired at t=%.0fs."
        % trigger_offset
    )
    return "\n".join(lines)


def sample(frames: Sequence[Frame], n: int) -> list[Frame]:
    """Evenly sample at most n frames, always keeping the first and last."""
    if len(frames) <= n:
        return list(frames)
    step = (len(frames) - 1) / (n - 1)
    picked = [frames[round(i * step)] for i in range(n)]
    # Dedupe while preserving order
    seen, out = set(), []
    for f in picked:
        if f.path not in seen:
            seen.add(f.path)
            out.append(f)
    return out


def run_gate(frames: Sequence[Frame], cfg: Config | None = None) -> GateResult | None:
    """Cheap screen. Returns None when the gate is disabled."""
    cfg = cfg or config
    backend = cfg.gate_backend.lower()
    if backend == "off":
        return None

    picked = sample(frames, 6)
    paths = [f.path for f in picked]
    log.info("gate: %d frames via %s", len(paths), backend)

    if backend == "ollama":
        return ask_ollama(paths, GATE_PROMPT, GateResult, cfg)
    return ask_claude(paths, GATE_PROMPT, GateResult,
                      model="claude-haiku-4-5", thinking=False, max_tokens=1024)


def run_verdict(frames: Sequence[Frame], trigger_offset: float,
                cfg: Config | None = None) -> Verdict:
    """Full determination over the event window."""
    cfg = cfg or config
    model = cfg.verdict_model.strip()
    local = model.lower() in ("ollama", "local")
    # The local model's context window fits about 8 frames; Claude takes 16.
    picked = sample(frames, 8 if local else 16)
    prompt = VERDICT_PROMPT.format(frame_index=_frame_index(picked, trigger_offset))
    paths = [f.path for f in picked]

    log.info("verdict: %d frames via %s", len(paths), model)

    if local:
        return ask_ollama(paths, prompt, Verdict, cfg)
    return ask_claude(paths, prompt, Verdict, model=model,
                      thinking=True, effort="high", max_tokens=8000)
