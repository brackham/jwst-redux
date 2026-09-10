from __future__ import annotations

import pytest


@pytest.fixture
def exposure_records() -> list[dict]:
    common = {
        "productLevel": "1b, 2a, 2b, 2c",
        "program": 5799,
        "visit": 1,
        "targprop": "TOI-3884",
        "targname": "TOI-3884",
        "targ_ra": 181.56,
        "targ_dec": 12.49,
        "instrume": "NIRISS",
        "exp_type": "NIS_SOSS",
        "tsovisit": "t",
        "opticalElements": "CLEAR;GR700XD",
        "filter": "CLEAR",
        "pupil": "GR700XD",
        "subarray": "SUBSTRIP256",
        "duration": 19405.888,
        "nints": 321,
        "ngroups": 10,
        "exsegtot": 3,
        "access": "PUBLIC",
        "pi_name": "Garcia, Lionel",
        "proposal_type": "GO",
    }
    return [
        {
            **common,
            "ArchiveFileID": 238871685,
            "fileSetName": "jw05799001001_04101_00001",
            "observtn": 1,
            "visit_id": "05799001001",
            "date_obs": "2025-06-02T08:19:23.1350000",
            "expstart": 60828.34679554399,
            "expend": 60828.57140072917,
        },
        {
            **common,
            "ArchiveFileID": 238874700,
            "fileSetName": "jw05799002001_04101_00001",
            "observtn": 2,
            "visit_id": "05799002001",
            "date_obs": "2025-06-15T23:33:58.1960000",
            "expstart": 60841.98192356482,
            "expend": 60842.20652875,
        },
    ]


@pytest.fixture
def product_records() -> list[dict]:
    records = []
    for dataset in ("jw05799001001_04101_00001", "jw05799002001_04101_00001"):
        for segment, size in ((3, 1_101_075_840), (1, 1_132_531_200), (2, 1_132_531_200)):
            filename = f"{dataset}-seg{segment:03d}_nis_uncal.fits"
            records.append(
                {
                    "product_key": filename,
                    "access": "PUBLIC",
                    "dataset": dataset,
                    "filename": filename,
                    "uri": f"{dataset}/{filename}",
                    "file_suffix": "_uncal",
                    "category": "1b",
                    "size": size,
                    "type": "science",
                }
            )
        records.append(
            {
                "product_key": f"{dataset}_rateints.fits",
                "access": "PUBLIC",
                "dataset": dataset,
                "filename": f"{dataset}-seg001_nis_rateints.fits",
                "uri": f"{dataset}/rateints.fits",
                "file_suffix": "_rateints",
                "category": "2a",
                "size": 42,
                "type": "science",
            }
        )
    return records
