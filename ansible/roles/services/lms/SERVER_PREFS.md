# Lyrion server preferences: what we pin, and how LMS treats each one

The `lms` role pins 21 server preferences (`defaults/main.yml` → `lms_server_prefs`)
on every deploy. This page explains what each one does inside Lyrion, based on the
code that actually runs. It was written for the music-library retag (#142), whose
tags these preferences interpret.

**Source researched:** Lyrion Music Server **9.1.2** (revision `1790142097`, built
2026-09-24), copied out of the running `lms` container on n5pro-docker on
2026-10-02. Citations are `file:line` in that tree (`/lms/` inside the container).
**INFERRED** marks a claim that was reasoned out but not read in the code or measured.
The container runs `:stable` with Watchtower auto-update, so after a major version
bump these line numbers drift. Re-check anything you are about to rely on.

## How the pin works

`tasks/server_prefs.yml`, run last in the role (or alone with `--tags lms_prefs`):

1. Waits for LMS JSON-RPC (`POST /jsonrpc.js` on the web port), then reads every
   pinned pref from the **running server** with `["pref", <name>, "?"]`.
2. Sends `["pref", <name>, <value>]` only for prefs whose live value differs.
3. Reads them all back and asserts they now match.

Why not edit `server.prefs`:

- **LMS keeps the file in memory and rewrites it.** Every successful `set` saves
  the whole in-memory hash 10 s later (`Slim/Utils/Prefs/Base.pm:125`,
  `Slim/Utils/Prefs/Namespace.pm:298-335`), and `writeAll` runs before every scan
  (`Slim/Music/Import.pm:122-124`). An edit made while LMS runs is overwritten.
- **A parse error wipes everything.** If the YAML fails to load, LMS starts with
  every server **and player** pref at its default and saves that over the file
  (`Namespace.pm:266-270`, `Base.pm:233`). Player prefs live in the same file
  (`Slim/Utils/Prefs/Client.pm:44`).
- **Loading the file runs nothing.** `YAML::XS::LoadFile` fills the hash directly
  (`Namespace.pm:84,266`), with no validators and no change handlers. A `splitList`
  edited while LMS is stopped would never queue the wipecache it needs.

What the `pref` command does (`Slim/Control/Commands.pm:2630-2675`) goes through
`Base.pm:83-166`:

- A scalar equal (`eq`) to the current value is a no-op (`Base.pm:95`).
- Otherwise LMS runs the validator, schedules the save, runs the change handlers,
  and sends a `prefset` notification.
- **It replies success even when a validator rejects the value.** Only `mediadirs`
  has a validator here (`Slim/Utils/Prefs.pm:383-403`), but this is why the role's
  read-back assert exists.
- It cannot set a pref back to undef.

The read-back shows what LMS **accepted**. The write to disk follows 10 s later,
so a container stop inside that window loses it, and the next run puts it back.

## Summary

| Pref | Pinned | LMS default | Read at | Changing it… |
| ---- | ------ | ----------- | ------- | ------------ |
| `dontTriggerScanOnPrefChange` | `1` | `1` | change time | 1→0 runs any queued scan now |
| `useUnifiedArtistsList` | `1` | `0` | browse | rebuilds menus, clears caches |
| `trackartistInArtists` | `0` | `0` | browse (live: unified is on) | clears caches |
| `composerInArtists` | `0` | `0` | browse (live: unified is on) | clears caches |
| `conductorInArtists` | `0` | `0` | browse (live: unified is on) | clears caches |
| `bandInArtists` | `0` | `0` | browse (live: unified is on) | clears caches |
| `artistAlbumLink`, `albumartistAlbumLink` | `1` | `1` | browse | clears caches |
| `trackartist`/`composer`/`conductor`/`bandAlbumLink` | `0` | derived (below) | browse | clears caches |
| `variousArtistAutoIdentification` | `1` | `1` | browse | clears caches |
| `variousArtistsString` | `Various Artists` | unset → translated "Various Artists" | browse + scan (VA object) | renames the VA row on next lookup |
| `useTPE2AsAlbumArtist` | `1` | `1` | **scan** (MP3 only) | **queues a wipecache** |
| `splitList` | `;` | `;` | **scan** | **queues a wipecache** |
| `mediadirs` | `[/music]` | `[/music]` in Docker | scan | removed dir: queued wipe; added dir: **immediate** scan of it |
| `ignoreDirRE` | `^extras$` | empty | **scan** + folder browse | clears a query cache only. **No rescan is queued** |
| `groupArtistAlbumsByReleaseType` | `1` | `0` | browse | clears the web page cache |

The first apply changed only `variousArtistsString` (unset → `Various Artists`)
and `groupArtistAlbumsByReleaseType` (0 → 1). `ignoreDirRE` was added later
(empty → `^extras$`, 2026-10-03), and `useUnifiedArtistsList` was switched 0 → 1
the same day (next section). The rest pin values that were already live.

## dontTriggerScanOnPrefChange

This is not one of the library settings. It is pinned because the role's own side
effects depend on it, so it is applied first.

- When a scan-affecting pref changes, its handler issues `wipecache`, plus `queue`
  when this pref is `1` (`Prefs.pm:411-414`).
- `wipecacheCommand` queues the wipe when `queue` is given, a scan is running, or
  queueing is active. Otherwise it starts a wipe scan immediately
  (`Commands.pm:3104-3127`).
- The queue is in memory (`Import.pm:81`), so a restart drops it (INFERRED).
- Queueing a wipecache clears any other queued work (`Import.pm:854-862`).
- A queued wipe runs:
  - after any scan finishes (`Import.pm:789-806`);
  - instead of any later `rescan`, whether from the CLI, the UI or the Rescan
    plugin (`Commands.pm:2713-2717`);
  - from the settings page's "rescan now" link (`Web/Settings.pm:139-140`);
  - when this pref is flipped 1→0 (`Prefs.pm:484-488`).

With it at `1`, correcting a drifted `splitList` never starts a multi-hour wipe on
its own. The role prints a warning instead, and the operator decides when to rescan.

## The artist list: useUnifiedArtistsList and the four *InArtists flags

**`useUnifiedArtistsList` is the master switch.** At `0` it makes the four
`*InArtists` flags almost irrelevant. At `1`, the pinned value since 2026-10-03,
they decide which extra roles join the list.

**Pinned at `1`: one "Artists" list of album owners.** The owner asked for this,
relayed by the music-cleanup session. Measured after the post-retag full rescan,
before the switch: "All Artists" listed 854 contributors and "Album Artists" 194.
The other 660 were guests, track artists on compilations and composers.

- **Which roles.** The unified list uses `activeContributorRoles(0)`
  (`Queries.pm:1116-1120`): ARTIST plus ALBUMARTIST, plus any role whose
  `*InArtists` flag is `1` (all four are pinned `0`), plus active user-defined
  roles (there are none). TRACKARTIST is not included unless
  `trackartistInArtists` is `1` (`Contributor.pm:158-168`).
- **Compilations.** With `variousArtistAutoIdentification` on, `$va_pref` is
  true (`Queries.pm:1039`). Contributors are then counted only through
  non-compilation albums (`:1212`), and one synthetic Various Artists entry is
  added (`:1273-1280`, `:1416-1432`). Its name follows `variousArtistsString`.
- **Menus.** The single "Artists" node (`BrowseLibrary.pm:508`) replaces "Album
  Artists" and "All Artists" (`:516-546`). ExtendedBrowseModes rebuilds its
  menus on the change (`Plugin/ExtendedBrowseModes/Plugin.pm:56`).
- **Genre drill-down and New/Recently Played Artists** drop their
  `role_id:ALBUMARTIST` filter in unified mode (`BrowseLibrary.pm:1312`,
  `Plugin.pm:292,306`), so they also follow `activeContributorRoles`.
- **Switching it starts no scan.** The change handlers are in-memory only:
  `initializeRoles`, `Slim::Schema->wipeCaches`, the XMLBrowser cache and the
  ExtendedBrowseModes menu rebuild.

The rest of this section describes the two-menu mode, what each role means and
why the flags are pinned.

- **Menus.** At `0` the home menu shows **Album Artists** (`role_id:ALBUMARTIST`)
  and **All Artists** (no role filter) (`Slim/Menu/BrowseLibrary.pm:516-546`). The
  single configurable "Artists" menu exists only at `1` (`:508`).
- **Album Artists list (`aa_merge`, `Slim/Control/Queries.pm:1037`).** Under a
  role filter of ALBUMARTIST at `0`, LMS also accepts the ARTIST role (`:1113`).
  It then drops artists whose albums are **all** compilations, but keeps the
  Various Artists contributor itself (`:1197-1212`). Album and track queries add
  ARTIST the same way (`Queries.pm:379,393,6501`).
- **All Artists** lists every contributor in every role, including composers,
  conductors, bands and track artists, whatever the flags say
  (`Queries.pm:1122-1126`).
- **Genre drill-down, "New Artists", "Recently Played Artists"** add
  `role_id:ALBUMARTIST` (`BrowseLibrary.pm:1312`,
  `Plugin/ExtendedBrowseModes/Plugin.pm:292,306`). "Top Artists" has no filter
  (`:274-280`).

**The `*InArtists` flags.**

- `initializeRoles` turns them into `@inArtistsRoles`
  (`Slim/Schema/Contributor.pm:109`). Only `activeContributorRoles()` reads that
  list (`:158-168`).
- Every browse caller of `activeContributorRoles()` sits behind a
  `useUnifiedArtistsList` check (`Queries.pm:381,1116`, `BrowseLibrary.pm:1105`,
  `Schema/ResultSet/Contributor.pm:48`). The settings page also hides these
  checkboxes when it is `0` (`Web/Settings/Server/Behavior.pm:146-149`).
- With `useUnifiedArtistsList` at `0`, they reach users in only two places:
  1. **Web Advanced Search** default role checkboxes
     (`Web/Pages/Search.pm:533-539`).
  2. **Contributor portrait scan.** It only looks up pictures for active roles
     (`Music/ContributorPictureScan.pm:88-105`), so composers and track artists
     get no local portrait.
- `bandInArtists` also lets `Album->artists()` fall back to BAND on an album with
  no ALBUMARTIST (`Schema/Album.pm:301`). That cannot happen once every album
  carries ALBUMARTIST.
- Changing any of the five runs only in-memory work: `initializeRoles`
  (`Prefs.pm:433`), `Slim::Schema->wipeCaches` (`Prefs.pm:573`,
  `Schema.pm:2302-2323`) and a menu-cache clear (`Web/XMLBrowser.pm:43-46`).
  **No rescan.** The scanner reads none of them.

**Why the flags are pinned.** With `useUnifiedArtistsList` at `1` they are live.
A Settings → Behaviour save writes all four, and any `1` would add that whole role
(every guest, composer, conductor or band) to the Artists list. Pinned at `0`,
the list stays album owners only.

## Album links on artist pages: the *AlbumLink prefs

These decide which roles produce an album link on an artist page (`allAlbumLinkRoles`,
`Contributor.pm:108`, used at `Queries.pm:811`). The `*InArtists` flags do not.

- The four role-specific defaults are expressions such as
  `useUnifiedArtistsList && composerInArtists` (`Prefs.pm:278-281`), but they are
  only stored when the pref is missing (`Base.pm:200`). After that they are frozen
  values, independent of the flags they were derived from.
- **Settings → Behaviour rewrites all six, and the four `*InArtists`, on every
  save**, hidden fields included (`Behavior.pm:88-92`). That is how they drift, so
  the role pins them at their 2026-10-02 live values: `artistAlbumLink`,
  `albumartistAlbumLink` = `1`, the other four = `0`.

## How a track's artist gets its role (no preference involved)

This decides who appears in Album Artists, so it matters for the retag even though
no preference controls it.

- **ARTIST becomes TRACKARTIST.** When a track has **both** ARTIST and ALBUMARTIST
  and `COMPILATION` is not explicitly 1/yes/true, the scanner renames ARTIST to
  TRACKARTIST (`Slim/Schema.pm:3044-3055,3120-3132`).
- **Guest artists stay out of Album Artists.** With an explicit ALBUMARTIST on
  every track, guest and featured artists become TRACKARTIST, which is not listed
  there.
- **ARTIST equal to ALBUMARTIST is harmless.** The same contributor holds both
  roles and is listed once.
- **One file without ALBUMARTIST is enough to leak.** Its ARTIST keeps the ARTIST
  role, and `aa_merge` then lists that artist under Album Artists.
- **COMPILATION=1 keeps ARTIST as ARTIST** (`:3120`). Those albums are filtered
  out of Album Artists (`Queries.pm:1210`).
- `contributor_album` roles are rebuilt from `contributor_track` without any
  preference (`Slim/Utils/Scanner/Local.pm:349-355`).

## Compilations: variousArtistAutoIdentification and variousArtistsString

### variousArtistAutoIdentification (`1`)

Never read at scan time. It only affects browsing.

- **`Album->artists()`** (`Schema/Album.pm:294-327`). The ALBUMARTIST role is
  tried first. With the pref on and the album a compilation, LMS skips the
  ARTIST-role fallback and returns the Various Artists object (`:307,313-315`).
  It therefore matters only for compilations **without** an ALBUMARTIST, which the
  retag eliminates.
- **Inert while `useUnifiedArtistsList` is `0`.** The synthetic "Various Artists"
  entry in the artist list needs both prefs on (`Queries.pm:1039,1416-1432`).

### variousArtistsString (`Various Artists`)

- The effective name is the pref, or else the translated `VARIOUSARTISTS` string
  (`Slim/Music/Info.pm:1540-1542`). That string is "Various Artists" in English
  (`strings.txt:18545`) but changes with the server `language` (German:
  "Diverse Interpreten", `:18544`).
- **The VA object.**
  - It is created or loaded at schema init (`Schema.pm:211`).
  - It is looked up by `namesearch` (upper-cased, `Utils/Text.pm:158`) and created
    with update-or-create (`Schema.pm:2093-2100`).
  - The server re-checks the name on every lookup and **renames the row** if the
    string changed (`:2105-2110`). The scanner caches it for the whole run (`:2087`).
  - The pref has no change handler.
- **A tagged ALBUMARTIST merges into the VA row, if spelled exactly the same.**
  - The tag's contributor is matched on exact `name = ?`
    (`Schema/Contributor.pm:282`, called from `Schema.pm:3154`). Whichever side
    creates the row first, the other finds it.
  - Surrounding spaces are stripped (`Schema.pm:3141`), but **case is not**:
    "Various artists" inserts a second contributor.
- **MusicBrainz ID.** With MUSICBRAINZ_ALBUMARTISTID set, LMS looks up by MBID
  first, then falls back to `name = ? AND musicbrainz_id IS NULL`. That matches the
  MBID-less VA row and writes the MBID onto it (`Contributor.pm:266-275,306-308`).
  MusicBrainz's VA id `89ad4ac3-39f7-470e-963a-56509c546377` therefore merges
  cleanly. A different MBID on the same name would create a separate row.
- **Why pin it.** On an English server, pinning the text it already resolves to
  changes nothing today. What it removes is the language dependency. Left unset, a
  change of `language` would rename the VA row, and every album tagged
  `ALBUMARTIST=Various Artists` would split off into a second contributor.

### How the compilation flag is set

- **`COMPILATION`** accepts 1/yes/true and 0/no/false (`Schema.pm:1779-1789`). In
  MP3 it comes from TCMP (`Formats/MP3.pm:72-73`).
- **ALBUMARTIST=VA with no COMPILATION tag.**
  - A full scan marks the album as a compilation through the "primary contributor
    is VA" rule (`Schema.pm:1289-1295`).
  - **Whether an incremental rescan keeps the flag is unresolved** (INFERRED,
    traced not run). The changed-file path resets `compilation` to undef and calls
    `mergeSingleVAAlbum` (`Utils/Scanner/Local.pm:1099-1105`). That function only
    counts ARTIST-role rows (`Schema.pm:2230`), and there are none once ARTIST has
    become TRACKARTIST, so it would write `compilation=0` (`:2281-2289`). But the
    source comments the reset itself as "XXX no longer works after album code
    ported to native DBI" (`:1100`), so it may never run. Which path wins was not
    tested. An explicit `COMPILATION=1` makes the question irrelevant.
- **COMPILATION=1 with a real ALBUMARTIST.**
  - The album is a compilation, but its contributor stays the real artist.
  - It shows under Various Artists (`Queries.pm:366-368`).
  - An artist whose albums are **all** compilations disappears from Album Artists
    (`Queries.pm:1212`).
- **RELEASETYPE=compilation never sets the flag.** It is dropped (`Schema.pm:1279`).

## useTPE2AsAlbumArtist (`1`)

- **MP3 only.** On tag mapping, TPE2 becomes ALBUMARTIST when the pref is on and
  BAND when it is off (`Formats/MP3.pm:317-322`).
  - The mapping overwrites (`:333-339`), so TPE2 replaces an `ALBUMARTIST` that is
    already set.
  - `TXXX:ALBUM ARTIST` also maps to ALBUMARTIST (`:61`). Which of the two wins
    depends on hash order (INFERRED nondeterministic).
- **FLAC is unaffected.** The MP3 mapper runs on a FLAC file only if it also
  carries an ID3 tag, and then without overwriting, so the Vorbis comments win
  (`Formats/FLAC.pm:235-239`).
- Read by the scanner, so a change **queues a wipecache** (`Prefs.pm:411-414`).

## splitList (`;`)

`Slim/Music/Info.pm:995-1060`, `splitTag`:

- **Format.** A whitespace-separated list of literal separators (`:1031`), matched
  with `\Q…\E` (`:1035`). No word boundaries, case-sensitive.
- **Separators do not combine.** Each is applied to the whole original value and
  every successful split is appended (`:1051-1054`). `"A & B; C"` with `"; &"`
  gives four contributors: `A & B`, `C`, `A`, `B; C`. This was verified by running
  a copy of the function.
- **Never add `&` or `and`.** `"Simon & Garfunkel"` → `Simon`, `Garfunkel`. And
  `and` matches inside words: `"Brandon Flowers"` → `Br`, `on Flowers`. Both
  verified. The only `&` exceptions are the genres "R&B" and "Rock & Roll"
  (`:1015-1017`).
- **Repeated tags bypass it.** A tag that arrives as a list (repeated Vorbis
  `ARTIST=` fields) is trimmed per element and **not split** (`:1008-1012`). So
  writing separate tag values, as the retag does, never depends on this pref.
  (That Audio::Scan hands repeated fields over as a list is INFERRED from the code
  comment.)
- **Applies to** every contributor role (ARTIST, ALBUMARTIST, TRACKARTIST,
  COMPOSER, CONDUCTOR, BAND, user roles) and their sort and MBID tags
  (`Schema.pm:3141-3153`, `Contributor.pm:77-90,241-250`). Also GENRE
  (`Schema/Genre.pm:101`) and RELEASETYPE (`Schema.pm:1279`).
- A change **queues a wipecache** (`Prefs.pm:411-413`). Until it runs, the library
  keeps the old splitting.

## mediadirs (`[/music]`)

- Read through `getMediaDirs`/`getAudioDirs` (`Slim/Utils/Misc.pm:705-742`). It
  must be a **list**: a bare scalar would break the dereference (INFERRED).
- The Docker default is `['/music']` when that folder exists
  (`Slim/Utils/OS/Docker.pm:26-28`).
- **Validator:** a list of unique entries that all exist as directories **inside
  the container** (`Prefs.pm:383-403`). If `/music` is unmounted when a correction
  runs, LMS rejects it and the role's assert fails (INFERRED for the NFS case).
- **Change handler** (`Prefs.pm:491-516`): a removed folder queues a wipecache, and
  an added folder triggers an **immediate** `rescan full` of that folder. Re-sending
  an identical list does nothing.
- Read-only `/music` is safe. Artwork goes to the cache dir and playlists to
  `playlistdir` (`Docker.pm:52-53`). No code was found writing into a media dir
  (INFERRED, the search was not exhaustive).

## ignoreDirRE (`^extras$`)

Keeps mcl's `extras/` folders (cue sheets, rip logs, bonus material filed beside
an album, `mcl/layout/names.py:73`) out of the library.

- **What it is matched against.** `fileFilter` tests every candidate's NAME, the
  basename of both files and folders (`Slim/Utils/Misc.pm:840-842`). It is never
  matched against the full path. The test is `$item =~ /$ignore/`: a Perl regex,
  unanchored unless you anchor it, case-sensitive.
  - `^extras$` therefore excludes an entry named exactly `extras` at any depth.
  - It does **not** exclude `Extras`, `extras (1)`, or a path like
    `extras/Disc 1` matched as a whole.
  - mcl writes the lowercase name. As of 2026-10-03 no `extras` folder existed
    yet in `/music` at any case (checked to depth 6).
- **A matching folder is pruned with everything under it.** The scanner walks
  with `folderFilter` → `fileFilter` (`Slim/Utils/Scanner.pm:119-123`,
  `Utils/Scanner/Local/Async.pm:63-67`).
- **The same filter applies elsewhere.** The local-artwork search
  (`Slim/Music/Artwork.pm:135`), the Music Folder browse view
  (`Slim/Utils/Misc.pm:1008`) and the Linux auto-rescan watcher
  (`Utils/AutoRescan/Linux.pm:130,144`) all use it.
- **A change triggers no rescan.** Its only change handler is
  `Slim::Control::Queries->wipeCaches`, which resets an in-memory query cache
  (`Prefs.pm:575-577`, `Control/Queries.pm:6098-6103`). Tracks already scanned
  from a newly excluded folder stay in the library until the next rescan. The role
  prints its own warning when it corrects this pref, because the general
  "queued a wipecache" warning would be false here.
- **No validator.** An invalid regex is accepted by `pref` and would then throw
  wherever `fileFilter` runs (INFERRED from `$item =~ /$ignore/`, not tested).
  Check a new pattern with `perl -e '"x" =~ /PATTERN/'` before pinning it.
- **Default is empty** (`Prefs.pm:145`). Only the Synology OS module sets one
  (`Utils/OS/Synology.pm:86-88`).

## groupArtistAlbumsByReleaseType (`1`)

- **Values** (`strings.txt:10330-10358`): `0` flat list, `1` group the album list
  of a **single artist**, `2` group every album list.
- **When grouping applies** (`BrowseLibrary.pm:1439-1452`): `ignoreReleaseTypes`
  is off, the view is filtered by `artist_id`, it is not under a Work, and the role
  filter is absent or ALBUMARTIST/COMPOSER.
- **Groups** (`Slim/Menu/BrowseLibrary/Releases.pm`):
  - Release types in the order Album, EP, Single, Broadcast, Other, Compilation,
    then custom types alphabetically (`:159-167`, `Schema/Album.pm:62-68`).
  - Albums with the **compilation flag** go to Compilations (`:108-110`).
  - Plus Appearances, per-role credits, Works and "All Releases" (`:181-261`).
- **One group stays flat.** If an artist ends up with a single group, LMS opens it
  directly (`Releases.pm:237-239`).
- **How a release type is stored at scan time** (`Schema.pm:1279-1280`,
  `Utils/Text.pm:54`):
  - LMS takes the first RELEASETYPE value that is not "compilation", upper-cases
    it and strips punctuation.
  - An untagged album becomes `ALBUM`.
  - MusicBrainz `album; compilation` becomes ALBUM.
  - `album/live` stays as a single custom type, because `/` is not a separator.
  - In FLAC, `MUSICBRAINZ_ALBUMTYPE` **overwrites** RELEASETYPE
    (`Formats/FLAC.pm:52-53,242-246`).
- **`releaseTypesToIgnore` is not applied to the groups.** Each group filters by
  its own type (`BrowseLibrary.pm:1485`), and only "All Releases" honours the
  ignore list.
- **Browse-time only.** It is in no wipecache list. Its only change hook clears the
  web page cache (`Web/XMLBrowser.pm:43-46`). The web UI and Jive clients both go
  through BrowseLibrary; Material skin is INFERRED to do the same.

## What this means for the retag (#142)

- **Write `ALBUMARTIST=Various Artists`** with exactly that spelling and case, so it
  merges into LMS's VA object instead of creating a twin.
- **Also write `COMPILATION=1` on VA albums.** Without it, the flag is proven only
  for full wipe scans. Whether an incremental rescan keeps it is unresolved
  (above). The explicit tag takes the rescan path out of the question.
- **MP3s: put the same value in TPE2 and `TXXX:ALBUM ARTIST`**, because which one
  wins is not deterministic.
- **FLAC: do not let `MUSICBRAINZ_ALBUMTYPE` disagree with `RELEASETYPE`**, because
  the former silently wins.
- **Release types group as written.** `RELEASETYPE` values album/ep/single/other
  map to the groups as-is. A `compilation` RELEASETYPE does **not** put an album in
  the Compilations group; the COMPILATION flag does.
- **Keep writing separate tag values for multiple artists.** That path never
  consults `splitList`.
- **None of the pinned changes needed a rescan.** The full rescan after the retag
  is still required, for the tag changes themselves.

## Verification record (2026-10-02, n5pro-docker)

- **Dry run** (`--check`) found exactly the two expected drifts:
  `variousArtistsString: null → "Various Artists"` and
  `groupArtistAlbumsByReleaseType: "0" → "1"`. The other 18 matched.
- **First apply** sent those two `pref` commands and the read-back assert passed.
  - On disk 10 s later, `server.prefs` differed from a pre-apply copy in exactly
    those two keys and their `_ts_` timestamps.
  - The container ID was unchanged and no scan started (`rescan ?` → 0).
- **Second run:** `changed=0`, no `pref` command sent.
- **Falsification.** `-e` override with `mediadirs: ["/music","/music"]`:
  - LMS's validator rejected the duplicate, but `pref` still replied success.
  - The read-back assert failed with its own message.
  - `mediadirs` stayed `["/music"]`, its `_ts_` was unchanged, and no scan started.
  - That run also caught a wrong "queued a wipecache" warning for the rejected
    value. The warning now runs after the read-back and covers accepted
    corrections only.
- **Effect.** `browselibrary items mode:albums artist_id:<The Black Keys>` now
  returns Albums / EPs / Singles / Other / Compilations / Appearances / All
  Releases.
- **Not proven:** a real `mediadirs` correction, which means sending a JSON array
  that LMS accepts.
  - The falsification cannot tell "array arrived and was rejected as a duplicate"
    from "array arrived malformed and was rejected".
  - If the transport is wrong, the read-back assert fails loudly; it does not pass
    silently.
