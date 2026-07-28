"""Marketing reel pipeline — the demo scenario.

    poster (image, SeeDream 5 Pro)
        └──> clip (video, Seedance 2 Fast, image-to-video via first_frame_url)
    jingle (music, Suno V5.5)            # independent — runs in parallel with poster

The clip step feeds the poster's display_url into Seedance as ``first_frame_url``
— note the author never writes that field name: they say ``source_image=${poster}``
and the mapper picks the right physical field per model.

Run:
    VIBE_TOKEN=oc_... python -m examples.marketing_reel
or:
    vibe run examples/marketing_reel.yaml
"""

from __future__ import annotations
import asyncio
import os
import sys

from vibe import Pipeline, Step, VibeClient


def build() -> Pipeline:
    poster = Step(
        id="poster",
        type="image",
        model="seedream-5-pro",
        prompt="рекламный баннер салона красоты «САЛОН КРАСОТЫ», фиолетовый неон, крупный текст",
        params={"aspect_ratio": "1:1", "quality": "high", "output_format": "png"},
    )
    jingle = Step(
        id="jingle",
        type="music",
        model="suno-v5.5-instrumental",
        prompt="лёгкий upbeat pop-джингл для салона красоты, 15 секунд",
        params={"music_style": "pop", "style_tags": "upbeat, light"},
    )
    clip = Step(
        id="clip",
        type="video",
        model="seedance-2-mini",
        prompt="Кинематографичный кадр: камера медленно наезжает на светящийся неоновый баннер салона красоты, мягкое мерцание огней, тёмный фон с бликами, глубина резкости, плавное движение, атмосферное освещение",
        inputs={"source_image": "${poster}"},  # ← logical; mapper → first_frame_url
        params={"duration": 4, "aspect_ratio": "9:16", "resolution": "480p"},
    ).depends_on("poster")

    return Pipeline(steps=[poster, jingle, clip], budget_rub=480)


async def main() -> None:
    token = os.environ.get("VIBE_TOKEN")
    if not token:
        sys.exit("Set VIBE_TOKEN (your Agent API key from lk.vibemarketolog.ru/#agent)")
    async with VibeClient(token) as client:
        outputs = await build().run(client)
        for sid, url in outputs.items():
            print(f"{sid}: {url}")


if __name__ == "__main__":
    asyncio.run(main())
