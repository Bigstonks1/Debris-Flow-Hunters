# Contributing to Debris Flow Hunters

Thank you for helping. This project runs the USGS post-fire debris-flow models for any US
wildfire and puts the results in front of land and emergency managers. People may act on its
maps, so the bar is that every change keeps results traceable and comparable with USGS's own
assessments.

## Set up

Install [uv](https://docs.astral.sh/uv/), then:

```bash
git clone https://github.com/Bigstonks1/Debris-Flow-Hunters.git
cd Debris-Flow-Hunters
uv sync
```

`uv sync` installs Python 3.12 and the USGS packages `pfdf` and `wildcat` from the USGS
package index in `pyproject.toml`. Check that it works with one small fire:

```bash
uv run python pipeline/run_fire.py --id 2020-NMGNF-000232     # Tadpole 2020, about 2 minutes
```

## Find your way around

- `README.md`: what the project does and how to run it.
- `design.md`: how it works. §3 is the end-to-end workflow, §4 the inputs and their
  fallbacks, §6 a file-by-file map of the code, §9 known traps.
- `CLAUDE.md`: the same rules in short form, for AI coding assistants.

## Rules that keep results trustworthy

- **Do not change the USGS models.** wildcat and pfdf are the USGS's software. This project
  drives them and never copies, patches or reimplements them. Change behaviour through each
  project's `configuration.py` (template: `pipeline/wildcat_config_template.py`).
- **Run wildcat only through the pipeline** (`pipeline/run_wildcat.py` or
  `pipeline/measure.py`), never `wildcat assess` from the command line: they install a fix
  for an incompatibility between pysheds and NumPy 2 first.
- **Every input must be free and need no account, key or email.** A new source needs a
  fallback, and its choice must be written to the project's `sources.json` so the app can
  show it. Never fall back silently.
- **Identify fires by Unique Fire ID**, never by name: names repeat.
- **Never commit `data/`.** It holds downloads and results (0.1 to 4 GB per fire).
- **Keep secrets out of files.** Cluster passwords live in the operating system's password
  store (`app/hpc.py`).
- **Keep the stage banners** (`===== name =====`) in `pipeline/run_fire.py`: the app reads them.
- **Write for the users.** Anything a user sees should be in plain language for land and
  emergency managers.

## Check your change

There is no unit-test suite yet. GitHub checks every pull request automatically: it lints the
code with Ruff, installs the project and imports every module. Run the same locally before
you push: `uvx ruff@0.14.0 check .` and `uv run python tests/check_imports.py`. Once a week, and from the Actions tab on demand, it also runs Tadpole 2020 end to
end and checks it against USGS's assessment; a failure there is often a public data service
that changed or was briefly down. Depending on what you changed:

- **Anything that can change a result** (inputs, preprocessing, configuration, matching):
  rerun the validation against observed debris flows and include its output in your pull
  request. It takes about 30 minutes for 12 fires:
  ```bash
  uv run python pipeline/batch.py all tests/observed/fires.txt --name observed --runner local --workers 3 --force
  uv run python tests/observed/score_observed.py
  ```
  It prints any number that differs from the white paper. A difference is not automatically
  wrong, but explain it.
- **One fire end to end**: `uv run python pipeline/run_fire.py --id <Unique Fire ID>`, then
  look at `data/<fire>_*/usgs_check.json`. Its threshold ratio to USGS's assessment should
  stay near 1 (0.89 to 1.01 on the 12 validation fires).
- **The app**: `uv run python app/server.py`, open http://127.0.0.1:8000, run a fire and
  check the map and the exports.
- **Report results straight**, including negative ones. Benchmarks you run go in `design.md`
  §8 with their source, what was measured, why and how.

## Propose a change

1. Open an issue first for anything larger than a fix, so the approach can be agreed.
2. Make a branch from `main`, one branch per piece of work.
3. Match the surrounding code: its naming, comment style and structure. Keep changes small
   and focused.
4. Update `README.md`, `design.md` and `CLAUDE.md` when you change how something is run or
   what it does.
5. Open a pull request against `main` that says what changed, why, and how you checked it.

## Report a problem

Open an issue with the fire's Unique Fire ID, the command or app step, what you expected,
what happened, and the end of the fire's log (`data/batches/<batch>/logs/` or the app's
**Logs** panel). If results disagree with a USGS assessment, include
`data/<fire>_*/usgs_check.json`.

## License

By contributing, you agree that your contribution is licensed under the GNU General Public
License v3.0 (GPL-3.0-only), the project's license (`LICENSE`). Only contribute code you
wrote or may license under GPL-3.0, and keep credit for any data source you add in the
README's **Data credits**.
