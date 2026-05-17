.PHONY: test test-v lint compile

# ROS Jazzy의 PYTHONPATH가 venv(py3.10)에 py3.12 site-packages를 흘려 보내
# pytest 플러그인 검증이 터지므로, 자동 플러그인 로드를 끈다.
PY := PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python

test:
	$(PY) -m pytest tests/ -q

test-v:
	$(PY) -m pytest tests/ -v

lint:
	$(PY) -m black --check .

compile:
	$(PY) -m py_compile $$(find . -name '*.py' -not -path './.venv/*' -not -path './__pycache__/*' -not -path '*/__pycache__/*')
