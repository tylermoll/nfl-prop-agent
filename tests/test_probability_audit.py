import joblib
import numpy as np
import pytest

from scripts.audit_probability_pipeline import artifact_audit, probability_trace


def test_trace_uses_actual_minus_prediction_and_strict_over_tail(tmp_path):
    path = tmp_path / "artifact.joblib"
    # The boundary prediction belongs to the upper bucket because production uses
    # searchsorted(..., side="right").  A residual tied at the cutoff is not Over.
    joblib.dump({"pipeline": object(), "features": [],
                 "calibration_predictions": np.array([1., 2., 2., 3.]),
                 "calibration_residuals": np.array([-1., 2.5, 3., 4.]),
                 "prediction_bin_edges": [-np.inf, 2., np.inf]}, path)
    report = artifact_audit(path)
    trace = probability_trace(report, point=2., line=5.)
    assert trace["residual_bucket"] == 1
    assert trace["effective_calibration_n"] == 3
    assert trace["over_tail_residuals"] == [4.]
    assert trace["over_probability"] == 1 / 3
    assert trace["under_probability"] == pytest.approx(2 / 3)


def test_egbuka_reported_probability_implies_two_of_37_over_tail():
    # 5.4% rounded to one decimal is exactly the ECDF step produced by 2/37.
    over = 2 / 37
    assert round(100 * over, 1) == 5.4
    assert round(100 * (1 - over), 1) == 94.6
