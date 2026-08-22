# Real footage fixtures

Drop real camera clips here (`.MOV`/`.MP4` pairs, or singles) to exercise
`tests/integration/test_real_footage.py` against actual extraction and
`mlx-vlm` inference -- no mocking, real `ffmpeg`/`qlmanage`/model calls.

- Already covered by the repo's blanket `.gitignore` rule for `*.MOV`/`*.mp4`
  -- files placed here are never committed, regardless of size.
- The integration test auto-skips if this directory is empty, so an empty
  checkout doesn't fail anywhere else's test run.
- A couple of representative clips is enough (a static shot, a pan, low
  light) -- this isn't meant to be a full camera dump.
