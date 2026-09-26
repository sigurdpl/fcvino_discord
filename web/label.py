"""Read a wine's label from a photograph.

Registering eight bottles for an evening means typing eight long names into a
phone. A photograph of the front label gets most of it, and a person checks the
result before anything is saved — a decorative label misread is a plausible
mistake, and the club records vintages it would rather have blank than wrong.

Why not a barcode: the number carries no information about the wine, so it is
only as good as the catalogue behind it, and no free catalogue covers this
cellar. Open Food Facts has Gato Negro and Cloudy Bay but not Produttori del
Barbaresco; Vinmonopolet's open data was cut back in 2021 and its barcodes sit
on the partner side. A label works on a Barolo, which is the point.

The photograph is read from the request, sent, and dropped: it is never saved
in the club's own files and never logged. (A large upload spends the request in
one of Starlette's temporary files, which it deletes when the request ends.)
"""

from __future__ import annotations

import base64
import logging

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"

# A phone photograph is a megabyte or two; more than this is not a label.
MAX_BYTES = 5 * 1024 * 1024
ALLOWED_TYPES = ("image/jpeg", "image/png", "image/webp", "image/heic", "image/heif")

PROMPT = """This is a photograph of a wine bottle's label.

Read what the label actually says and fill in the fields. Leave a field null
when the label does not say — a guess is worse than a blank box here, because
somebody will trust it. In particular do not infer a vintage that is not printed,
and do not infer a grape from the region.

`name` is how the club would write the wine down: producer and cuvée and
vintage, as on the label. `producer` is the estate alone. Keep the label's own
spelling and accents."""


class Label(BaseModel):
    """What a label can tell us, in the cellar's own columns.

    Every field is required and nullable rather than optional: the model has to
    say "the label doesn't give one" about each, which is a harder thing to skip
    past than simply leaving a key out.
    """

    name: str | None = Field(description="The wine as written down, with vintage")
    producer: str | None = Field(description="The estate alone")
    vintage: int | None = Field(description="Only if printed on the label")
    country: str | None = Field(description="Only if the label says")
    region: str | None = Field(description="The appellation or district on the label")
    grape: str | None = Field(description="Only if the label says")


class LabelUnreadable(Exception):
    """The photograph could not be turned into a wine. The message is shown."""


def read_label(image: bytes, media_type: str, api_key: str) -> Label:
    """Ask the model what bottle this is. Raises LabelUnreadable on any failure."""
    import anthropic

    if len(image) > MAX_BYTES:
        raise LabelUnreadable("That photo is too big — try again, or type it in.")
    if media_type not in ALLOWED_TYPES:
        raise LabelUnreadable("That isn't a photo. Pick an image, or type it in.")

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.parse(
            model=MODEL,
            max_tokens=1024,
            # Reading a label is a plain extraction, not a problem to think about.
            output_config={"effort": "low"},
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.standard_b64encode(image).decode(),
                        },
                    },
                    {"type": "text", "text": PROMPT},
                ],
            }],
            output_format=Label,
        )
    except anthropic.APIStatusError as exc:
        log.warning("label reading failed: %s", exc.status_code)
        raise LabelUnreadable("Could not reach the label reader. Type it in?") from exc
    except anthropic.APIConnectionError as exc:
        raise LabelUnreadable("No connection to the label reader. Type it in?") from exc

    label = response.parsed_output
    if label is None or not (label.name or "").strip():
        raise LabelUnreadable("Couldn't read a wine off that one. Try again, or type it in.")
    return label
