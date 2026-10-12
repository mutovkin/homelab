# sacd_extract

SACD ISO → DSF/DFF extractor for the music-cleanup tool on workbench CTs (#301).
Opt in per host with `sacd_extract_enabled: true`; the version pin lives in
`defaults/main.yml`.

**Built from source** at a pinned commit of
[EuFlo/sacd-ripper](https://github.com/EuFlo/sacd-ripper) (GPL-2.0), the
maintained fork. The release's prebuilt Linux binary links `libxml2.so.2`, and
Ubuntu 26.04 ships only `libxml2.so.16`. Measured 2026-10-12: the prebuilt binary
installed cleanly and `ldd` reported `libxml2.so.2 => not found`, so it would
have failed at mcl's first extraction.

Two guards, each graded by the evidence it actually has:

- **The release tag must resolve to the pinned SHA on the remote**
  (`git ls-remote`), so the two pins cannot drift apart, and a `--check` of a
  pin bump evaluates it too. Seen red under `--check` with
  `-e sacd_extract_release=0.3.9.3` (that tag exists but resolves to aab60976)
  and with a tag that does not exist (`<no such tag>`).
- **`--version`, run through the PATH link, must print exactly
  `sacd_extract client <release>` on its own line.** It is anchored because the
  build stamps `git describe --tags --dirty`: an untagged commit or a dirty tree
  adds a suffix, and a failed describe stamps `NA`. Before the tag guard existed,
  this check went red live with `-e sacd_extract_release=9.9.9z`. Now the tag
  guard fires first on any wrong release value, so the anchoring was tested
  OFFLINE instead. A release VALUE of `0.3.9.3` against the real 0.3.9.3b
  output fails the anchored match. So does the expected value `0.3.9.3b`
  against output edited to read like an untagged build (`0.3.9.3b-4-g…`). The
  first revision's substring test passed both.

## Calling it (measured on CT 202, 2026-10-12, 0.3.9.3b)

- **Read stdout through a PIPE, never a redirected file.** With stdout
  redirected to a regular file it writes ONE byte (a newline) and exits **0**,
  whatever the option order. Through a pipe the same `-P` run wrote 3505 bytes
  and all 9 tracks. Python's `subprocess` capture uses a pipe and is fine; a
  shell `> file` silently loses everything.
- **`-o DIR` must already exist.** Otherwise the tool prints `output dir doesn't
  exist or is not a directory` and exits 255.
- It reads `sacd_extract.cfg` from its working directory and prints that
  directory, so run it from a directory you control.
- `sacd_extract -2 -s -i <iso> -o <dir>` extracts the stereo area to DSF. On a
  2014 SHM-SACD (DSD64, 9 tracks, 2.06 GB ISO) it took 5 s and wrote
  `<dir>/<album>/NN - Title.dsf`. ffprobe reads `dsd_lsbf_planar`, 2 ch, and
  track 1's duration matches the disc's TOC.
