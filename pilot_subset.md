# SWE-bench Verified — Pilot Subset (HIVE-288)

**30 instances · 12 repos · seed=42 · deterministic.** Regenerate verbatim with
`python3 select_pilot_subset.py` (pulls the live `princeton-nlp/SWE-bench_Verified`
dataset; stdlib only, no pip). Pinned machine-readable form: `pilot_subset.json`.

## Why this subset
The pilot validates the index→retrieve→patch→score pipeline + methodology before
the full run (HIVE-289), so breadth matters more than volume:

- **Stratified by repo, capped at 4/repo** — exercises per-instance codebase
  indexing across *all 12* Verified repos instead of over-weighting django
  (which alone is 231/500 = 46% of the full set).
- **Difficulty spread** — spans every annotated bucket, including both of the
  dataset's only-3 `>4 hours` instances, so the pilot is not all-trivial.

## Full-set distribution (for reference)
| Repo | Count (of 500) | In pilot |
|---|--:|--:|
| django/django | 231 | 4 |
| sympy/sympy | 75 | 4 |
| sphinx-doc/sphinx | 44 | 4 |
| matplotlib/matplotlib | 34 | 3 |
| scikit-learn/scikit-learn | 32 | 3 |
| astropy/astropy | 22 | 2 |
| pydata/xarray | 22 | 2 |
| pytest-dev/pytest | 19 | 2 |
| pylint-dev/pylint | 10 | 2 |
| psf/requests | 8 | 2 |
| mwaskom/seaborn | 2 | 1 |
| pallets/flask | 1 | 1 |

Difficulty across the full 500: `15 min - 1 hour` 261 · `<15 min fix` 194 · `1-4 hours` 42 · `>4 hours` 3.

## The 30 instances
| instance_id | difficulty | repo |
|---|---|---|
| astropy__astropy-7336 | <15 min fix | astropy/astropy |
| astropy__astropy-7606 | 15 min - 1 hour | astropy/astropy |
| django__django-11239 | <15 min fix | django/django |
| django__django-13401 | 15 min - 1 hour | django/django |
| django__django-13449 | 1-4 hours | django/django |
| django__django-14915 | <15 min fix | django/django |
| matplotlib__matplotlib-13989 | <15 min fix | matplotlib/matplotlib |
| matplotlib__matplotlib-14623 | 15 min - 1 hour | matplotlib/matplotlib |
| matplotlib__matplotlib-24177 | <15 min fix | matplotlib/matplotlib |
| mwaskom__seaborn-3187 | 15 min - 1 hour | mwaskom/seaborn |
| pallets__flask-5014 | <15 min fix | pallets/flask |
| psf__requests-1142 | <15 min fix | psf/requests |
| psf__requests-2931 | 15 min - 1 hour | psf/requests |
| pydata__xarray-4075 | <15 min fix | pydata/xarray |
| pydata__xarray-6938 | 15 min - 1 hour | pydata/xarray |
| pylint-dev__pylint-6528 | 15 min - 1 hour | pylint-dev/pylint |
| pylint-dev__pylint-6903 | <15 min fix | pylint-dev/pylint |
| pytest-dev__pytest-7205 | <15 min fix | pytest-dev/pytest |
| pytest-dev__pytest-8399 | 15 min - 1 hour | pytest-dev/pytest |
| scikit-learn__scikit-learn-11578 | 15 min - 1 hour | scikit-learn/scikit-learn |
| scikit-learn__scikit-learn-13135 | <15 min fix | scikit-learn/scikit-learn |
| scikit-learn__scikit-learn-25102 | 1-4 hours | scikit-learn/scikit-learn |
| sphinx-doc__sphinx-7590 | >4 hours | sphinx-doc/sphinx |
| sphinx-doc__sphinx-7910 | <15 min fix | sphinx-doc/sphinx |
| sphinx-doc__sphinx-8548 | 1-4 hours | sphinx-doc/sphinx |
| sphinx-doc__sphinx-8593 | 15 min - 1 hour | sphinx-doc/sphinx |
| sympy__sympy-13480 | <15 min fix | sympy/sympy |
| sympy__sympy-13878 | >4 hours | sympy/sympy |
| sympy__sympy-17630 | 1-4 hours | sympy/sympy |
| sympy__sympy-20438 | 15 min - 1 hour | sympy/sympy |
