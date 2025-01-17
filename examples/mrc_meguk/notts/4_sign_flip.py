"""Sign flipping.

"""

from glob import glob
from dask.distributed import Client

from osl import source_recon, utils

# Authors : Rukuang Huang <rukuang.huang@jesus.ox.ac.uk>
#           Chetan Gohil <chetan.gohil@psych.ox.ac.uk>

TASK = "resteyesopen"  # resteyesopen or resteyesclosed

# Setup FSL
source_recon.setup_fsl("/well/woolrich/projects/software/fsl")

# Directories
SRC_DIR = f"/well/woolrich/projects/mrc_meguk/notts/{TASK}/src"

if __name__ == "__main__":
    utils.logger.set_up(level="INFO")
    client = Client(n_workers=16, threads_per_worker=1)

    # Subjects to sign flip
    subjects = []
    for path in sorted(glob(SRC_DIR + "/*/parc/parc-raw.fif")):
        subject = path.split("/")[-3]
        subjects.append(subject)

    # Find a good template subject to align other subjects to
    template = source_recon.find_template_subject(
        SRC_DIR, subjects, n_embeddings=15, standardize=True
    )

    # Settings
    config = f"""
        source_recon:
        - fix_sign_ambiguity:
            template: {template}
            n_embeddings: 15
            standardize: True
            n_init: 3
            n_iter: 3000
            max_flips: 20
    """

    # Do the sign flipping
    source_recon.run_src_batch(config, SRC_DIR, subjects, dask_client=True)
