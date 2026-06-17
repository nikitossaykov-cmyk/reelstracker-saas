"""Strategy MakeUGC — UGC-style perfume reel pipeline.

Ported from /opt/tg-bot-mimic/tools/reel_factory/bin/ (helper-bot R&D
through 2026-06-17). Each module is a discrete pipeline stage that the
makeugc_worker calls in order: portrait → voiceover → lipsync × N →
cutaway → concat. This scaffold PR ships only the portrait stage to
validate the architecture in Railway end-to-end; remaining stages land
as separate PRs.

Pricing assumption (per reel, 2026-06-17):
  flux-kontext-max  ~$0.04 × 3 portraits   ≈ $0.12
  prunaai/p-video-avatar  $0.025/s × 30s   ≈ $0.75
  ElevenLabs IVC                          ≈ $0.10 (shared quota)
  ffmpeg concat/cutaway                    free (own CPU)
  total                                    ≈ $1.50 / reel
"""
