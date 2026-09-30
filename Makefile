PYTHON ?= python3
ROOT := $(CURDIR)
export PYTHONPATH := $(ROOT)
export MPLCONFIGDIR := /tmp/udf-artifact-matplotlib
export PYTHONDONTWRITEBYTECODE := 1

.PHONY: verify quick test smoke controlled-sqlite analyze-controlled

verify:
	sha256sum -c MANIFEST.sha256
	$(PYTHON) scripts/validate/validate_release.py
	$(PYTHON) scripts/validate/validate_strengthening.py

quick:
	$(PYTHON) scripts/validate/validate_release.py
	$(PYTHON) scripts/validate/validate_strengthening.py

test:
	$(PYTHON) -m unittest discover -s tests -v

smoke:
	rm -rf /tmp/udf-artifact-smoke-output
	mkdir -p /tmp/udf-artifact-smoke-output
	$(PYTHON) workloads/synthetic/generate.py --family fixed_loop --rows 10 --seed 42 --loop-bounds 2 3 --output /tmp/udf_artifact_smoke.csv
	ARTIFACT_OUTPUT_ROOT=/tmp/udf-artifact-smoke-output $(PYTHON) scripts/run/experiment_runner.py --system sqlite --workload fixed_loop --scale 10 --experiment-id artifact-smoke --seed 42 --loop-bounds 2 3 --warmups 0 --repetitions 3 --timeout 30 --dataset /tmp/udf_artifact_smoke.csv

controlled-sqlite:
	rm -rf /tmp/udf-controlled-output
	mkdir -p /tmp/udf-controlled-output
	ARTIFACT_OUTPUT_ROOT=/tmp/udf-controlled-output $(PYTHON) scripts/run/run_e1_e2.py --family both --warmups 3 --repetitions 10 --timeout 60

analyze-controlled:
	ARTIFACT_OUTPUT_ROOT=/tmp/udf-controlled-output $(PYTHON) scripts/analyze/analyze_results.py
	ARTIFACT_OUTPUT_ROOT=/tmp/udf-controlled-output $(PYTHON) scripts/analyze/analyze_e1_e2.py
