.PHONY: test test-v lint compile

# ROS Jazzy injects py3.12 site-packages into the py3.10 venv PYTHONPATH,
# which breaks pytest's plugin validation. Disable autoload to dodge it.
PY := PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python

test:
	$(PY) -m pytest tests/ -q

test-v:
	$(PY) -m pytest tests/ -v

lint:
	$(PY) -m black --check .

compile:
	$(PY) -m py_compile main.py robot_bridge.py bitrate_controller.py station_bridge.py state.py stream_telemetry.py wire_format.py config/schema.py config/__init__.py zenoh_utils/__init__.py zenoh_utils/session.py
