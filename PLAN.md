# Plan

What's left from the code review of 2026-09-18. Everything else from that review is done (commit `16b119d`).

## Keep the editor's own changes on re-run

- [ ] `_ensure_clip` (`timeline.py`) deletes *all* properties on our two bin clips and forces `kdenlive:folderid = -1`. It should replace only the properties it owns, so a bin folder, a favourite star or markers survive a re-run.
- [ ] Keep a user-adjusted overlay position. If the `qtblend` rect on our overlay entry differs from what the tool last wrote, reuse it instead of resetting it. Swooshes can stay regenerated, but document that in the README.

## Speed and memory

- [ ] Stream decoded audio into Silero window by window instead of buffering the whole voice track (`vad.decode`/`speech_windows`). That takes memory from about 77 MB per player to almost nothing, which matters for long episodes or 8 players.

## Tidy-up

- [ ] Remove the unused fields `Track.index` and `Track.tractor` (`timeline.py`).
- [ ] Annotate `die()` as `-> NoReturn` (`config.py`).
- [ ] `"{%s}" % uuid.uuid4()` → f-string (`timeline.py`).
- [ ] Keep `fps` as a `Fraction` straight from the profile, exact for 59.94/29.97, and drop `limit_denominator(1001)` in `render.py`.

## Before publishing

- [ ] README: replace `git clone … &&` with `https://github.com/seetee/indicate-speaker.git`.
- [ ] Commit `uv.lock` (remove it from `.gitignore`) so installs get the tested versions.
- [ ] GitHub Actions: `uv run pytest` on the latest versions, plus `uv run --resolution lowest-direct pytest` to keep the minimum versions honest (they were verified by hand on 2026-09-18), plus `ruff check`.
- [ ] `pyproject.toml`: `readme`, `authors`, project URLs and classifiers; add `--version` to the CLI.
- [ ] Privacy: use generic player names and paths in `CLAUDE.md`, `theme.example.toml` and the tests, instead of the crew's names and `~/documents/samkvam.online`/`~/bin/gearlever_*`.
- [ ] Tests: audio decoding with a small generated file (seek and trim alignment of `vad.decode`).

## Future options (only if needed)

- [ ] An optional `[speech]` theme section for the detector thresholds (`ON`/`OFF`, hangover, minimum burst), for a mic that behaves differently.
- [ ] Crop non-square avatars to a centred square instead of stretching them (`render.py`, `_image`).
- [ ] README note: clips with Kdenlive speed effects (time remapping) get slightly shifted speech timing.
