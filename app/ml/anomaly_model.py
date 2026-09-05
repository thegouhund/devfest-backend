"""Model deteksi anomali vital sign (Isolation Forest, dilatih di devfest-ml).

Dimuat sekali saat modul pertama diimpor — bundle 3MB dan `joblib.load`
tidak murah untuk dipanggil per-request. `functools.lru_cache` memberi
efek singleton yang sama dengan `get_settings()` di app/core/config.py.

Ambang keputusan dan urutan fitur ikut bundle, bukan hardcode di sini:
salah urutan kolom membuat scaler membaca fitur yang salah tanpa error.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import joblib
import pandas as pd


MODEL_PATH = Path(__file__).parent / "models" / "health_anomaly_model.pkl"


@dataclass(frozen=True)
class MlDetection:
    """Hasil satu prediksi model. `score` semakin besar semakin anomali —
    kebalikan dari `decision_function` mentah scikit-learn, supaya
    pemanggil tidak perlu tahu konvensi tanda Isolation Forest."""

    is_anomaly: bool
    score: float
    threshold: float


@lru_cache(maxsize=1)
def _bundle() -> dict:
    return joblib.load(MODEL_PATH)


def feature_order() -> list[str]:
    """Urutan kolom yang diharapkan scaler/model, apa adanya dari bundle."""
    return list(_bundle()["features"])


def predict(features: dict[str, float]) -> MlDetection:
    """Jalankan satu prediksi.

    `features` harus memuat seluruh nama di `feature_order()`; nama ekstra
    diabaikan `pandas`, tapi nama yang kurang akan melempar `KeyError` —
    sengaja keras, karena fitur yang diam-diam jadi NaN membuat model
    menebak tanpa peringatan.
    """
    bundle = _bundle()
    columns = bundle["features"]
    frame = pd.DataFrame([{col: features[col] for col in columns}])

    # RobustScaler.transform mengembalikan ndarray polos; dibungkus balik
    # jadi DataFrame supaya IsolationForest tidak mengeluh kehilangan nama
    # kolom yang dipakainya saat training.
    scaled = pd.DataFrame(bundle["scaler"].transform(frame), columns=columns)
    raw_score = float(bundle["model"].decision_function(scaled)[0])

    # IsolationForest.decision_function: skor RENDAH/negatif = lebih anomali.
    # Dibalik jadi "makin besar makin anomali" supaya konsisten dengan
    # deviation_score z-score yang digantikannya (lihat services/anomaly.py).
    score = -raw_score
    threshold = abs(float(bundle["optimal_threshold"]))

    return MlDetection(is_anomaly=score > threshold, score=score, threshold=threshold)
