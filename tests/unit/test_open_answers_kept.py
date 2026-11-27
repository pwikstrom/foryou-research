"""Open answers are kept as given: no label becomes "other category" per file.

Until 2026-11 the cleaning step replaced the labels beyond those covering 95%
of a file's labels. In a small file nearly every open answer occurs once, so
the labels it removed were an arbitrary pick that depended on file size.
"""

import pandas as pd

from fyp.annotation.annotation_refinement import clean_up_machine_annotations
from fyp.core.fyp_config import fyp_cf


def test_every_open_answer_survives_a_small_file():
    other = fyp_cf["labels"]["OTHER_THINGS"]
    activities = [f"doing thing number {i}" for i in range(50)]
    objects = [[f"object {i}", f"item {i}", "phone"] for i in range(50)]
    df = pd.DataFrame(
        {
            "item_id": [str(i) for i in range(50)],
            "main_activity": activities,
            "objects": objects,
        }
    )

    out = clean_up_machine_annotations(df)

    assert out["main_activity"].tolist() == activities
    assert [list(x) for x in out["objects"]] == objects
    assert other not in set(out["main_activity"])
