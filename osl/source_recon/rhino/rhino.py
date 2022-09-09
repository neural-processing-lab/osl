#!/usr/bin/env python

"""Registration of Headshapes Including Nose in OSL (RHINO).

"""

# Authors: Mark Woolrich <mark.woolrich@ohba.ox.ac.uk>
#          Chetan Gohil <chetan.gohil@psych.ox.ac.uk>

import warnings
import os
import os.path as op
from copy import deepcopy

import numpy as np
import nibabel as nib
import matplotlib.pyplot as plt

from mne import read_epochs, read_forward_solution
from mne.viz._3d import _sensor_shape
from mne.viz.backends.renderer import _get_renderer
from mne.transforms import (
    write_trans,
    read_trans,
    apply_trans,
    _get_trans,
    combine_transforms,
    Transform,
    rotation,
    invert_transform,
)
from mne.forward import _create_meg_coils
from mne.io import _loc_to_coil_trans, read_info, read_raw
from mne.io.pick import pick_types
from mne.io.constants import FIFF
from mne.surface import read_surface, write_surface
from mne.source_space import _make_volume_source_space, _complete_vol_src

import osl.source_recon.rhino.utils as rhino_utils
from osl.source_recon.rhino.surfaces import get_surfaces_filenames
from osl.source_recon.rhino.freesurfer import create_freesurfer_mesh
from osl.utils.logger import log_or_print


def get_coreg_filenames(subjects_dir, subject):
    """
    Generates a dict of files generated and used by RHINO.

    Parameters
    ----------
    subjects_dir : string
        Directory to put RHINO subject dirs in.
        Files will be in subjects_dir/subject/rhino/coreg/
    subject : string
        Subject name dir to put RHINO files in.
        Files will be in subjects_dir/subject/rhino/coreg/

    Returns
    -------
    filenames : dict
        A dict of files generated and used by RHINO.
    """
    basedir = op.join(subjects_dir, subject, "rhino", "coreg")
    os.makedirs(basedir, exist_ok=True)

    filenames = {
        "basedir": basedir,
        "fif_file": op.join(basedir, "data-raw.fif"),
        "smri_file": op.join(basedir, "smri.nii.gz"),
        "head_mri_t_file": op.join(basedir, "head_mri-trans.fif"),
        "ctf_head_mri_t_file": op.join(basedir, "ctf_head_mri-trans.fif"),
        "mrivoxel_mri_t_file": op.join(basedir, "mrivoxel_mri_t_file-trans.fif"),
        "smri_nasion_file": op.join(basedir, "smri_nasion.txt"),
        "smri_rpa_file": op.join(basedir, "smri_rpa.txt"),
        "smri_lpa_file": op.join(basedir, "smri_lpa.txt"),
        "polhemus_nasion_file": op.join(basedir, "polhemus_nasion.txt"),
        "polhemus_rpa_file": op.join(basedir, "polhemus_rpa.txt"),
        "polhemus_lpa_file": op.join(basedir, "polhemus_lpa.txt"),
        "polhemus_headshape_file": op.join(basedir, "polhemus_headshape.txt"),
        "forward_model_file": op.join(basedir, "forward-fwd.fif"),
        "std_brain": os.environ["FSLDIR"]
        + "/data/standard/MNI152_T1_1mm_brain.nii.gz",
    }

    return filenames


def coreg(
    fif_file,
    subjects_dir,
    subject,
    use_headshape=True,
    use_nose=True,
    use_dev_ctf_t=True,
    logger=None,
):
    """Coregistration.

    Calculates a linear, affine transform from native sMRI space
    to polhemus (head) space, using headshape points that include the nose
    (if useheadshape = True).

    Requires rhino.compute_surfaces to have been run.

    This is based on the OSL Matlab version of RHINO.

    Call get_coreg_filenames(subjects_dir, subject) to get a file list
    of generated files.

    RHINO firsts registers the polhemus-derived fiducials (nasion, rpa, lpa)
    in polhemus space to the sMRI-derived fiducials in native sMRI space.

    RHINO then refines this by making use of polhemus-derived headshape points
    that trace out the surface of the head (scalp), and ideally include
    the nose.

    Finally, these polhemus-derived headshape points in polhemus space are
    registered to the sMRI-derived scalp surface in native sMRI space.

    In more detail:

    1) Map location of fiducials in MNI standard space brain to native sMRI
    space. These are then used as the location of the sMRI-derived fiducials
    in native sMRI space.
    2) We have polhemus-derived fids in polhemus space and sMRI-derived fids
    in native sMRI space. We use these to estimate the affine xform from
    native sMRI space to polhemus (head) space.
    3) We have the polhemus-derived headshape points in polhemus
    space and the sMRI-derived headshape (scalp surface) in native sMRI space.
    We use these to estimate the affine xform from native sMRI space using the
    ICP algorithm initilaised using the xform estimate in step 2.

    Parameters
    ----------
    fif_file : string
        Full path to MNE-derived fif file.
    subjects_dir : string
        Directory to put RHINO subject dirs in.
        Files will be in subjects_dir/subject/rhino/coreg/
    subject : string
        Subject name dir to put RHINO files in.
        Files will be in subjects_dir/subject/rhino/coreg/
    use_headshape : bool
        Determines whether polhemus derived headshape points are used.
    use_nose : bool
        Determines whether nose is used to aid coreg, only relevant if
        useheadshape=True
    use_dev_ctf_t : bool
        Determines whether to set dev_head_t equal to dev_ctf_t
        in fif_file's info. This option is only potentially
        needed for fif files originating from CTF scanners. Will be
        ignored if dev_ctf_t does not exist in info (e.g. if the data
        is from a MEGIN scanner)
    logger : logging.getLogger
        Logger.
    """

    # Note the jargon used varies for xforms and coord spaces:
    # MEG (device) -- dev_head_t --> HEAD (polhemus)
    # HEAD (polhemus)-- head_mri_t (polhemus2native) --> MRI (native)
    # MRI (native) -- mri_mrivoxel_t (native2nativeindex) --> MRI (native) voxel indices
    #
    # RHINO does everthing in mm

    log_or_print("*** RUNNING OSL RHINO COREGISTRATION ***", logger)

    filenames = get_coreg_filenames(subjects_dir, subject)
    surfaces_filenames = get_surfaces_filenames(subjects_dir, subject)

    if use_headshape:
        if use_nose:
            log_or_print(
                "The MRI-derived nose is going to be used to aid coreg.",
                logger,
            )
            log_or_print(
                "Please ensure that rhino.compute_surfaces was run with include_nose=True.",
                logger,
            )
            log_or_print(
                "Please ensure that the polhemus headshape points include the nose.",
                logger,
            )
        else:
            log_or_print(
                "The MRI-derived nose is not going to be used to aid coreg.",
                logger,
            )
            log_or_print(
                "Please ensure that the polhemus headshape points do not include the nose",
                logger,
            )

    # Load in the "polhemus-derived fiducial points"
    log_or_print(f"loading: {filenames['polhemus_headshape_file']}", logger)
    polhemus_headshape = np.loadtxt(filenames["polhemus_headshape_file"])

    log_or_print(f"loading: {filenames['polhemus_nasion_file']}", logger)
    polhemus_nasion = np.loadtxt(filenames["polhemus_nasion_file"])

    log_or_print(f"loading: {filenames['polhemus_rpa_file']}", logger)
    polhemus_rpa = np.loadtxt(filenames["polhemus_rpa_file"])

    log_or_print(f"loading: {filenames['polhemus_lpa_file']}", logger)
    polhemus_lpa = np.loadtxt(filenames["polhemus_lpa_file"])

    # Load in outskin_mesh_file to get the "sMRI-derived headshape points"
    if use_nose:
        outskin_mesh_file = surfaces_filenames["bet_outskin_plus_nose_mesh_file"]
    else:
        outskin_mesh_file = surfaces_filenames["bet_outskin_mesh_file"]

    smri_headshape_nativeindex = rhino_utils.niimask2indexpointcloud(outskin_mesh_file)

    # -------------------------------------------------------------------------
    # Copy fif_file to new file for modification, and (optionally) changes
    # dev_head_t to equal dev_ctf_t in fif file info

    if fif_file[-7:] == "raw.fif":
        raw = read_raw(fif_file)
    elif fif_file[-10:] == "epochs.fif":
        raw = read_epochs(fif_file)
    else:
        raise ValueError(
            "Invalid fif file, needs to be a *raw.fif or a *epochs.fif file"
        )

    if use_dev_ctf_t:
        dev_ctf_t = raw.info["dev_ctf_t"]
        if dev_ctf_t is not None:
            log_or_print("CTF data", logger)
            log_or_print(
                "Setting dev_head_t equal to dev_ctf_t in fif file info.",
                logger,
            )
            log_or_print("To turn this off, set use_dev_ctf_t=False", logger)
            dev_head_t, _ = _get_trans(raw.info["dev_head_t"], "meg", "head")
            dev_head_t["trans"] = dev_ctf_t["trans"]

    raw.save(filenames["fif_file"], overwrite=True)
    fif_file = filenames["fif_file"]

    # -------------------------------------------------------------------------
    # 1) Map location of fiducials in MNI standard space brain to native sMRI
    # space. These are then used as the location of the sMRI-derived fiducials
    # in native sMRI space.

    # Known locations of MNI derived fiducials in MNI coords in mm
    mni_nasion_mni = np.asarray([1, 85, -41])
    mni_rpa_mni = np.asarray([83, -20, -65])
    mni_lpa_mni = np.asarray([-83, -20, -65])

    mni_mri_t = read_trans(surfaces_filenames["mni_mri_t_file"])

    # Apply this xform to the mni fids to get what we call the "sMRI-derived
    # fids" in native space
    smri_nasion_native = rhino_utils.xform_points(mni_mri_t["trans"], mni_nasion_mni)
    smri_lpa_native = rhino_utils.xform_points(mni_mri_t["trans"], mni_lpa_mni)
    smri_rpa_native = rhino_utils.xform_points(mni_mri_t["trans"], mni_rpa_mni)

    # -------------------------------------------------------------------------
    # 2) We have polhemus-derived fids in polhemus space and sMRI-derived fids
    # in native sMRI space. We use these to estimate the affine xform from
    # native sMRI space to polhemus (head) space.

    # Note that smri_fid_native are the sMRI-derived fids in native space
    polhemus_fid_polhemus = np.concatenate(
        (
            np.reshape(polhemus_nasion, [-1, 1]),
            np.reshape(polhemus_rpa, [-1, 1]),
            np.reshape(polhemus_lpa, [-1, 1]),
        ),
        axis=1,
    )
    smri_fid_native = np.concatenate(
        (
            np.reshape(smri_nasion_native, [-1, 1]),
            np.reshape(smri_rpa_native, [-1, 1]),
            np.reshape(smri_lpa_native, [-1, 1]),
        ),
        axis=1,
    )

    # Estimate the affine xform from native sMRI space to polhemus (head) space
    xform_native2polhemus = rhino_utils.rigid_transform_3D(
        polhemus_fid_polhemus, smri_fid_native
    )

    # Now we can transform sMRI-derived headshape pnts into polhemus space:

    # get native (mri) voxel index to native (mri) transform
    xform_nativeindex2native = rhino_utils._get_sform(outskin_mesh_file)["trans"]

    # put sMRI-derived headshape points into native space (in mm)
    smri_headshape_native = rhino_utils.xform_points(
        xform_nativeindex2native, smri_headshape_nativeindex
    )

    # put sMRI-derived headshape points into polhemus space
    smri_headshape_polhemus = rhino_utils.xform_points(
        xform_native2polhemus, smri_headshape_native
    )

    # -------------------------------------------------------------------------
    # 3) We have the polhemus-derived headshape points in polhemus
    # space and the sMRI-derived headshape (scalp surface) in native sMRI space.
    # We use these to estimate the affine xform from native sMRI space using the
    # ICP algorithm initilaised using the xform estimate in step 2.

    if use_headshape:
        log_or_print("Running ICP...", logger)

        # Run ICP with multiple initialisations to refine registration of
        # sMRI-derived headshape points to polhemus derived headshape points,
        # with both in polhemus space

        # Combined polhemus-derived headshape points and polhemus-derived fids,
        # with them both in polhemus space
        # These are the "source" points that will be moved around
        polhemus_headshape_4icp = np.concatenate(
            (polhemus_headshape, polhemus_fid_polhemus), axis=1
        )

        xform_icp, err, e = rhino_utils.rhino_icp(
            smri_headshape_polhemus, polhemus_headshape_4icp, 30
        )

    else:
        # No refinement by ICP:
        xform_icp = np.eye(4)

    # Put sMRI-derived headshape points into ICP "refined" polhemus space
    xform_native2polhemus_refined = np.linalg.inv(xform_icp) @ xform_native2polhemus
    smri_headshape_polhemus = rhino_utils.xform_points(
        xform_native2polhemus_refined, smri_headshape_native
    )

    # put sMRI-derived fiducials into refined polhemus space
    smri_nasion_polhemus = rhino_utils.xform_points(
        xform_native2polhemus_refined, smri_nasion_native
    )
    smri_rpa_polhemus = rhino_utils.xform_points(
        xform_native2polhemus_refined, smri_rpa_native
    )
    smri_lpa_polhemus = rhino_utils.xform_points(
        xform_native2polhemus_refined, smri_lpa_native
    )

    # -------------------------------------------------------------------------
    # Save coreg info

    # save xforms in MNE format in mm
    xform_native2polhemus_refined_copy = np.copy(xform_native2polhemus_refined)

    head_mri_t = Transform(
        "head", "mri", np.linalg.inv(xform_native2polhemus_refined_copy)
    )
    write_trans(filenames["head_mri_t_file"], head_mri_t, overwrite=True)

    nativeindex_native_t = np.copy(xform_nativeindex2native)
    mrivoxel_mri_t = Transform("mri_voxel", "mri", nativeindex_native_t)
    write_trans(filenames["mrivoxel_mri_t_file"], mrivoxel_mri_t, overwrite=True)

    # save sMRI derived fids in mm in polhemus space
    np.savetxt(filenames["smri_nasion_file"], smri_nasion_polhemus)
    np.savetxt(filenames["smri_rpa_file"], smri_rpa_polhemus)
    np.savetxt(filenames["smri_lpa_file"], smri_lpa_polhemus)

    # -------------------------------------------------------------------------
    # Create sMRI-derived surfaces in native/mri space in mm,
    # for use by forward modelling

    create_freesurfer_mesh(
        infile=surfaces_filenames["bet_inskull_mesh_vtk_file"],
        surf_outfile=surfaces_filenames["bet_inskull_surf_file"],
        nii_mesh_file=surfaces_filenames["bet_inskull_mesh_file"],
        xform_mri_voxel2mri=mrivoxel_mri_t["trans"],
    )

    create_freesurfer_mesh(
        infile=surfaces_filenames["bet_outskull_mesh_vtk_file"],
        surf_outfile=surfaces_filenames["bet_outskull_surf_file"],
        nii_mesh_file=surfaces_filenames["bet_outskull_mesh_file"],
        xform_mri_voxel2mri=mrivoxel_mri_t["trans"],
    )

    create_freesurfer_mesh(
        infile=surfaces_filenames["bet_outskin_mesh_vtk_file"],
        surf_outfile=surfaces_filenames["bet_outskin_surf_file"],
        nii_mesh_file=surfaces_filenames["bet_outskin_mesh_file"],
        xform_mri_voxel2mri=mrivoxel_mri_t["trans"],
    )

    log_or_print("*** OSL RHINO COREGISTRATION COMPLETE ***", logger)


def coreg_display(
    subjects_dir,
    subject,
    plot_type="surf",
    display_outskin_with_nose=False,
    display_sensors=True,
    filename=None,
):
    """Display coregistration.

    Displays the coregistered RHINO scalp surface and polhemus/sensor locations

    Display is done in MEG (device) space (in mm).

    Purple dots are the polhemus derived fiducials (these only get used to
    initialse the coreg, if headshape points are being used).

    Yellow diamonds are the MNI standard space derived fiducials (these are the
    ones that matter)

    Parameters
    ----------
    subjects_dir : string
        Directory to put RHINO subject dirs in.
        Files will be in subjects_dir/subject/rhino/coreg/
    subject : string
        Subject name dir to put RHINO files in.
        Files will be in subjects_dir/subject/rhino/coreg/
    plot_type : string
        Either:
            'surf' to do a 3D surface plot using surface meshes
            'scatter' to do a scatter plot using just point clouds
    display_outskin_with_nose : bool
        Whether to include nose with scalp surface in the display
    display_sensors : bool
        Whether to include sensors in the display
    filename : str
        Filename to save display to (as an interactive html).
        Must have extension .html.
    """

    # Note the jargon used varies for xforms and coord spaces:
    # MEG (device) -- dev_head_t --> HEAD (polhemus)
    # HEAD (polhemus)-- head_mri_t (polhemus2native) --> MRI (native)
    # MRI (native) -- mri_mrivoxel_t (native2nativeindex) --> MRI (native) voxel indices
    #
    # RHINO does everthing in mm

    surfaces_filenames = get_surfaces_filenames(subjects_dir, subject)

    bet_outskin_plus_nose_mesh_file = surfaces_filenames[
        "bet_outskin_plus_nose_mesh_file"
    ]
    bet_outskin_plus_nose_surf_file = surfaces_filenames[
        "bet_outskin_plus_nose_surf_file"
    ]
    bet_outskin_mesh_file = surfaces_filenames["bet_outskin_mesh_file"]
    bet_outskin_mesh_vtk_file = surfaces_filenames["bet_outskin_mesh_vtk_file"]
    bet_outskin_surf_file = surfaces_filenames["bet_outskin_surf_file"]

    coreg_filenames = get_coreg_filenames(subjects_dir, subject)
    head_mri_t_file = coreg_filenames["head_mri_t_file"]
    mrivoxel_mri_t_file = coreg_filenames["mrivoxel_mri_t_file"]

    smri_nasion_file = coreg_filenames["smri_nasion_file"]
    smri_rpa_file = coreg_filenames["smri_rpa_file"]
    smri_lpa_file = coreg_filenames["smri_lpa_file"]
    polhemus_nasion_file = coreg_filenames["polhemus_nasion_file"]
    polhemus_rpa_file = coreg_filenames["polhemus_rpa_file"]
    polhemus_lpa_file = coreg_filenames["polhemus_lpa_file"]
    polhemus_headshape_file = coreg_filenames["polhemus_headshape_file"]

    fif_file = coreg_filenames["fif_file"]

    if display_outskin_with_nose:
        outskin_mesh_file = bet_outskin_plus_nose_mesh_file
        outskin_mesh_4surf_file = bet_outskin_plus_nose_mesh_file
        outskin_surf_file = bet_outskin_plus_nose_surf_file
    else:
        outskin_mesh_file = bet_outskin_mesh_file
        outskin_mesh_4surf_file = bet_outskin_mesh_vtk_file
        outskin_surf_file = bet_outskin_surf_file

    # -------------------------------------------------------------------------
    # Setup xforms

    info = read_info(fif_file)

    mrivoxel_mri_t = read_trans(mrivoxel_mri_t_file)

    head_mri_t = read_trans(head_mri_t_file)
    # get meg to head xform in metres from info
    dev_head_t, _ = _get_trans(info["dev_head_t"], "meg", "head")

    # Change xform from metres to mm.
    # Note that MNE xform in fif.info assume metres, whereas we want it
    # in mm. To change units for an xform, just need to change the translation
    # part and leave the rotation alone
    dev_head_t["trans"][0:3, -1] = dev_head_t["trans"][0:3, -1] * 1000

    # We are going to display everything in MEG (device) coord frame in mm
    head_trans = invert_transform(dev_head_t)
    meg_trans = Transform("meg", "meg")
    mri_trans = invert_transform(
        combine_transforms(dev_head_t, head_mri_t, "meg", "mri")
    )

    # -------------------------------------------------------------------------
    # Setup fids and headshape points

    # Load, these are in mm
    polhemus_nasion = np.loadtxt(polhemus_nasion_file)
    polhemus_rpa = np.loadtxt(polhemus_rpa_file)
    polhemus_lpa = np.loadtxt(polhemus_lpa_file)
    polhemus_headshape = np.loadtxt(polhemus_headshape_file)

    # Move to MEG (device) space
    polhemus_nasion_meg = rhino_utils.xform_points(
        head_trans["trans"], polhemus_nasion
    )
    polhemus_rpa_meg = rhino_utils.xform_points(
        head_trans["trans"], polhemus_rpa
    )
    polhemus_lpa_meg = rhino_utils.xform_points(
        head_trans["trans"], polhemus_lpa
    )
    polhemus_headshape_meg = rhino_utils.xform_points(
        head_trans["trans"], polhemus_headshape
    )

    # Load sMRI derived fids, these are in mm in polhemus/head space
    smri_nasion_polhemus = np.loadtxt(smri_nasion_file)
    smri_rpa_polhemus = np.loadtxt(smri_rpa_file)
    smri_lpa_polhemus = np.loadtxt(smri_lpa_file)

    # Move to MEG (device) space
    smri_nasion_meg = rhino_utils.xform_points(
        head_trans["trans"], smri_nasion_polhemus
    )
    smri_rpa_meg = rhino_utils.xform_points(head_trans["trans"], smri_rpa_polhemus)
    smri_lpa_meg = rhino_utils.xform_points(head_trans["trans"], smri_lpa_polhemus)

    # -------------------------------------------------------------------------
    # Setup MEG sensors

    meg_picks = pick_types(info, meg=True, ref_meg=False, exclude=())

    coil_transs = [_loc_to_coil_trans(info["chs"][pick]["loc"]) for pick in meg_picks]
    coils = _create_meg_coils([info["chs"][pick] for pick in meg_picks], acc="normal")

    meg_rrs, meg_tris = list(), list()
    offset = 0
    for coil, coil_trans in zip(coils, coil_transs):
        rrs, tris = _sensor_shape(coil)
        rrs = apply_trans(coil_trans, rrs)
        meg_rrs.append(rrs)
        meg_tris.append(tris + offset)
        offset += len(meg_rrs[-1])
    if len(meg_rrs) == 0:
        print("MEG sensors not found. Cannot plot MEG locations.")
    else:
        meg_rrs = apply_trans(meg_trans, np.concatenate(meg_rrs, axis=0))
        meg_tris = np.concatenate(meg_tris, axis=0)

    # convert to mm
    meg_rrs = meg_rrs * 1000

    # -------------------------------------------------------------------------
    # Do plots

    if plot_type == "surf":
        warnings.filterwarnings("ignore", category=Warning)

        # Initialize figure
        renderer = _get_renderer(None, bgcolor=(0.5, 0.5, 0.5), size=(500, 500))

        # Polhemus-derived headshape points
        if len(polhemus_headshape_meg.T) > 0:
            polhemus_headshape_megt = polhemus_headshape_meg.T
            color, scale, alpha = (0, 0.7, 0.7), 0.007, 1
            renderer.sphere(
                center=polhemus_headshape_megt,
                color=color,
                scale=scale * 1000,
                opacity=alpha,
                backface_culling=True,
            )

        # MRI-derived nasion, rpa, lpa
        if len(smri_nasion_meg.T) > 0:
            color, scale, alpha = (1, 1, 0), 0.09, 1
            for data in [smri_nasion_meg.T, smri_rpa_meg.T, smri_lpa_meg.T]:
                transform = np.eye(4)
                transform[:3, :3] = mri_trans["trans"][:3, :3] * scale * 1000
                # rotate around Z axis 45 deg first
                transform = transform @ rotation(0, 0, np.pi / 4)
                renderer.quiver3d(
                    x=data[:, 0],
                    y=data[:, 1],
                    z=data[:, 2],
                    u=1.0,
                    v=0.0,
                    w=0.0,
                    color=color,
                    mode="oct",
                    scale=scale,
                    opacity=alpha,
                    backface_culling=True,
                    solid_transform=transform,
                )

        # Polhemus-derived nasion, rpa, lpa
        if len(polhemus_nasion_meg.T) > 0:
            color, scale, alpha = (1, 0, 1), 0.012, 1.5
            for data in [polhemus_nasion_meg.T, polhemus_rpa_meg.T, polhemus_lpa_meg.T]:
                renderer.sphere(
                    center=data,
                    color=color,
                    scale=scale * 1000,
                    opacity=alpha,
                    backface_culling=True,
                )

        if display_sensors:
            # Sensors
            if len(meg_rrs) > 0:
                color, alpha = (0.0, 0.25, 0.5), 0.2
                surf = dict(rr=meg_rrs, tris=meg_tris)
                renderer.surface(
                    surface=surf, color=color, opacity=alpha, backface_culling=True
                )

        # sMRI-derived scalp surface
        # if surf file does not exist, then we must create it
        create_freesurfer_mesh(
            infile=outskin_mesh_4surf_file,
            surf_outfile=outskin_surf_file,
            nii_mesh_file=outskin_mesh_file,
            xform_mri_voxel2mri=mrivoxel_mri_t["trans"],
        )

        coords_native, faces = nib.freesurfer.read_geometry(outskin_surf_file)

        # Move to MEG (device) space
        coords_meg = rhino_utils.xform_points(mri_trans["trans"], coords_native.T).T

        surf_smri = dict(rr=coords_meg, tris=faces)

        renderer.surface(
            surface=surf_smri, color=(1, 0.8, 1), opacity=0.4, backface_culling=False
        )

        renderer.set_camera(
            azimuth=90, elevation=90, distance=600, focalpoint=(0.0, 0.0, 0.0)
        )

        # Save or show
        rhino_utils.save_or_show_renderer(renderer, filename)

    # -------------------------------------------------------------------------
    elif plot_type == "scatter":

        # -------------------
        # Setup scalp surface

        # Load in scalp surface
        # And turn the nvoxx x nvoxy x nvoxz volume into a 3 x npoints point cloud
        smri_headshape_nativeindex = rhino_utils.niimask2indexpointcloud(
            outskin_mesh_file
        )
        # Move from native voxel indices to native space coordinates (in mm)
        smri_headshape_native = rhino_utils.xform_points(
            mrivoxel_mri_t["trans"], smri_headshape_nativeindex
        )
        # Move to MEG (device) space
        smri_headshape_meg = rhino_utils.xform_points(
            mri_trans["trans"], smri_headshape_native
        )

        plt.figure()
        ax = plt.axes(projection="3d")

        if display_sensors:
            color, scale, alpha, marker = (0.0, 0.25, 0.5), 1, 0.1, "."
            if len(meg_rrs) > 0:
                meg_rrst = meg_rrs.T  # do plot in mm
                ax.scatter(
                    meg_rrst[0, :],
                    meg_rrst[1, :],
                    meg_rrst[2, :],
                    color=color,
                    marker=marker,
                    s=scale,
                    alpha=alpha,
                )

        color, scale, alpha, marker = (0.5, 0.5, 0.5), 1, 0.2, "."
        if len(smri_headshape_meg) > 0:
            smri_headshape_megt = smri_headshape_meg
            ax.scatter(
                smri_headshape_megt[0, 0:-1:20],
                smri_headshape_megt[1, 0:-1:20],
                smri_headshape_megt[2, 0:-1:20],
                color=color,
                marker=marker,
                s=scale,
                alpha=alpha,
            )

        color, scale, alpha, marker = (0, 0.7, 0.7), 10, 0.7, "o"
        if len(polhemus_headshape_meg) > 0:
            polhemus_headshape_megt = polhemus_headshape_meg
            ax.scatter(
                polhemus_headshape_megt[0, :],
                polhemus_headshape_megt[1, :],
                polhemus_headshape_megt[2, :],
                color=color,
                marker=marker,
                s=scale,
                alpha=alpha,
            )

        if len(smri_nasion_meg) > 0:
            color, scale, alpha, marker = (1, 1, 0), 200, 1, "d"
            for data in (smri_nasion_meg, smri_rpa_meg, smri_lpa_meg):
                datat = data
                ax.scatter(
                    datat[0, :],
                    datat[1, :],
                    datat[2, :],
                    color=color,
                    marker=marker,
                    s=scale,
                    alpha=alpha,
                )

        if len(polhemus_nasion_meg) > 0:
            color, scale, alpha, marker = (1, 0, 1), 400, 1, "."
            for data in (polhemus_nasion_meg, polhemus_rpa_meg, polhemus_lpa_meg):
                datat = data
                ax.scatter(
                    datat[0, :],
                    datat[1, :],
                    datat[2, :],
                    color=color,
                    marker=marker,
                    s=scale,
                    alpha=alpha,
                )

        if filename is None:
            plt.show()
        else:
            plt.savefig(filename)
            plt.close()
    else:
        raise ValueError("invalid plot_type.")

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("ignore", Warning)


def bem_display(
    subjects_dir,
    subject,
    plot_type="scatter",
    display_outskin_with_nose=True,
    display_sensors=False,
    filename=None,
):
    """Displays the coregistered RHINO scalp surface and inner skull surface.

    Display is done in MEG (device) space (in mm).

    Parameters
    ----------
    subjects_dir : string
        Directory to find RHINO subject dirs in.
    subject : string
        Subject name dir to find RHINO files in.
    plot_type : string
        Either:
            'surf' to do a 3D surface plot using surface meshes
            'scatter' to do a scatter plot using just point clouds
    display_outskin_with_nose : bool
        Whether to include nose with scalp surface in the display
    display_sensors : bool
        Whether to include sensor locations in the display
    filename : str
        Filename to save display to (as an interactive html).
        Must have extension .html.
    """

    # Note the jargon used varies for xforms and coord spaces:
    # MEG (device) -- dev_head_t --> HEAD (polhemus)
    # HEAD (polhemus)-- head_mri_t (polhemus2native) --> MRI (native)
    # MRI (native) -- mri_mrivoxel_t (native2nativeindex) --> MRI (native) voxel indices
    #
    # RHINO does everthing in mm

    surfaces_filenames = get_surfaces_filenames(subjects_dir, subject)

    bet_outskin_plus_nose_mesh_file = surfaces_filenames[
        "bet_outskin_plus_nose_mesh_file"
    ]
    bet_outskin_plus_nose_surf_file = surfaces_filenames[
        "bet_outskin_plus_nose_surf_file"
    ]
    bet_outskin_mesh_file = surfaces_filenames["bet_outskin_mesh_file"]
    bet_outskin_mesh_vtk_file = surfaces_filenames["bet_outskin_mesh_vtk_file"]
    bet_outskin_surf_file = surfaces_filenames["bet_outskin_surf_file"]
    bet_inskull_mesh_file = surfaces_filenames["bet_inskull_mesh_file"]
    bet_inskull_surf_file = surfaces_filenames["bet_inskull_surf_file"]

    coreg_filenames = get_coreg_filenames(subjects_dir, subject)
    head_mri_t_file = coreg_filenames["head_mri_t_file"]
    mrivoxel_mri_t_file = coreg_filenames["mrivoxel_mri_t_file"]

    fif_file = coreg_filenames["fif_file"]

    if display_outskin_with_nose:
        outskin_mesh_file = bet_outskin_plus_nose_mesh_file
        outskin_mesh_4surf_file = bet_outskin_plus_nose_mesh_file
        outskin_surf_file = bet_outskin_plus_nose_surf_file
    else:
        outskin_mesh_file = bet_outskin_mesh_file
        outskin_mesh_4surf_file = bet_outskin_mesh_vtk_file
        outskin_surf_file = bet_outskin_surf_file

    fwd_fname = get_coreg_filenames(subjects_dir, subject)["forward_model_file"]
    forward = read_forward_solution(fwd_fname)
    src = forward["src"]

    # -------------------------------------------------------------------------
    # Setup xforms

    info = read_info(fif_file)

    mrivoxel_mri_t = read_trans(mrivoxel_mri_t_file)

    # get meg to head xform in metres from info
    head_mri_t = read_trans(head_mri_t_file)
    dev_head_t, _ = _get_trans(info["dev_head_t"], "meg", "head")

    # Change xform from metres to mm.
    # Note that MNE xform in fif.info assume metres, whereas we want it
    # in mm. To change units on an xform, just need to change the translation
    # part and leave the rotation alone
    dev_head_t["trans"][0:3, -1] = dev_head_t["trans"][0:3, -1] * 1000

    # We are going to display everything in MEG (device) coord frame in mm
    meg_trans = Transform("meg", "meg")
    mri_trans = invert_transform(
        combine_transforms(dev_head_t, head_mri_t, "meg", "mri")
    )
    head_trans = invert_transform(dev_head_t)

    # -------------------------------------------------------------------------
    # Setup MEG sensors

    if display_sensors:
        meg_picks = pick_types(info, meg=True, ref_meg=False, exclude=())

        coil_transs = [
            _loc_to_coil_trans(info["chs"][pick]["loc"]) for pick in meg_picks
        ]
        coils = _create_meg_coils(
            [info["chs"][pick] for pick in meg_picks], acc="normal"
        )

        meg_rrs, meg_tris = list(), list()
        offset = 0
        for coil, coil_trans in zip(coils, coil_transs):
            rrs, tris = _sensor_shape(coil)
            rrs = apply_trans(coil_trans, rrs)
            meg_rrs.append(rrs)
            meg_tris.append(tris + offset)
            offset += len(meg_rrs[-1])
        if len(meg_rrs) == 0:
            print("MEG sensors not found. Cannot plot MEG locations.")
        else:
            meg_rrs = apply_trans(meg_trans, np.concatenate(meg_rrs, axis=0))
            meg_tris = np.concatenate(meg_tris, axis=0)

        # convert to mm
        meg_rrs = meg_rrs * 1000

    # -------------------------------------------------------------------------
    # Setup vol source grid points

    if src is not None:
        # stored points are in metres, convert to mm
        src_pnts = src[0]["rr"][src[0]["vertno"], :] * 1000

        # Move from head space to MEG (device) space
        src_pnts = rhino_utils.xform_points(head_trans["trans"], src_pnts.T).T

        print("Number of dipoles={}".format(src_pnts.shape[0]))

    # -------------------------------------------------------------------------
    # Do plots

    if plot_type == "surf":
        warnings.filterwarnings("ignore", category=Warning)

        # Initialize figure
        renderer = _get_renderer(None, bgcolor=(0.5, 0.5, 0.5), size=(500, 500))

        # Sensors
        if display_sensors:
            if len(meg_rrs) > 0:
                color, alpha = (0.0, 0.25, 0.5), 0.2
                surf = dict(rr=meg_rrs, tris=meg_tris)
                renderer.surface(
                    surface=surf, color=color, opacity=alpha, backface_culling=True
                )

        # sMRI-derived scalp surface
        create_freesurfer_mesh(
            infile=outskin_mesh_4surf_file,
            surf_outfile=outskin_surf_file,
            nii_mesh_file=outskin_mesh_file,
            xform_mri_voxel2mri=mrivoxel_mri_t["trans"],
        )

        coords_native, faces = nib.freesurfer.read_geometry(outskin_surf_file)

        # Move to MEG (device) space
        coords_meg = rhino_utils.xform_points(mri_trans["trans"], coords_native.T).T

        surf_smri = dict(rr=coords_meg, tris=faces)

        # plot surface
        renderer.surface(
            surface=surf_smri,
            color=(0.85, 0.85, 0.85),
            opacity=0.3,
            backface_culling=False,
        )

        # Inner skull surface
        # Load in surface, this is in mm
        coords_native, faces = nib.freesurfer.read_geometry(bet_inskull_surf_file)

        # Move to MEG (device) space
        coords_meg = rhino_utils.xform_points(mri_trans["trans"], coords_native.T).T

        surf_smri = dict(rr=coords_meg, tris=faces)

        # plot surface
        renderer.surface(
            surface=surf_smri,
            color=(0.25, 0.25, 0.25),
            opacity=0.25,
            backface_culling=False,
        )

        # vol source grid points
        if src is not None and len(src_pnts.T) > 0:
            color, scale, alpha = (1, 0, 0), 0.001, 1
            renderer.sphere(
                center=src_pnts,
                color=color,
                scale=scale * 1000,
                opacity=alpha,
                backface_culling=True,
            )

        renderer.set_camera(
            azimuth=90, elevation=90, distance=600, focalpoint=(0.0, 0.0, 0.0)
        )

        # Save or show
        rhino_utils.save_or_show_renderer(renderer, filename)

    # -------------------------------------------------------------------------
    elif plot_type == "scatter":

        # -------------------
        # Setup scalp surface

        # Load in scalp surface
        # And turn the nvoxx x nvoxy x nvoxz volume into a 3 x npoints point cloud
        smri_headshape_nativeindex = rhino_utils.niimask2indexpointcloud(
            outskin_mesh_file
        )
        # Move from native voxel indices to native space coordinates (in mm)
        smri_headshape_native = rhino_utils.xform_points(
            mrivoxel_mri_t["trans"], smri_headshape_nativeindex
        )
        # Move to MEG (device) space
        smri_headshape_meg = rhino_utils.xform_points(
            mri_trans["trans"], smri_headshape_native
        )

        # -------------------------
        # Setup inner skull surface

        # Load in inner skull surface
        # And turn the nvoxx x nvoxy x nvoxz volume into a 3 x npoints point cloud
        inner_skull_nativeindex = rhino_utils.niimask2indexpointcloud(
            bet_inskull_mesh_file
        )
        # Move from native voxel indices to native space coordinates (in mm)
        inner_skull_native = rhino_utils.xform_points(
            mrivoxel_mri_t["trans"], inner_skull_nativeindex
        )
        # Move to MEG (device) space
        inner_skull_meg = rhino_utils.xform_points(
            mri_trans["trans"], inner_skull_native
        )

        ax = plt.axes(projection="3d")

        # sensors
        if display_sensors:
            color, scale, alpha, marker = (0.0, 0.25, 0.5), 2, 0.2, "."
            if len(meg_rrs) > 0:
                meg_rrst = meg_rrs.T  # do plot in mm
                ax.scatter(
                    meg_rrst[0, :],
                    meg_rrst[1, :],
                    meg_rrst[2, :],
                    color=color,
                    marker=marker,
                    s=scale,
                    alpha=alpha,
                )

        # scalp
        color, scale, alpha, marker = (0.75, 0.75, 0.75), 6, 0.2, "."
        if len(smri_headshape_meg) > 0:
            smri_headshape_megt = smri_headshape_meg
            ax.scatter(
                smri_headshape_megt[0, 0:-1:20],
                smri_headshape_megt[1, 0:-1:20],
                smri_headshape_megt[2, 0:-1:20],
                color=color,
                marker=marker,
                s=scale,
                alpha=alpha,
            )

        # inner skull
        inner_skull_megt = inner_skull_meg
        color, scale, alpha, marker = (0.5, 0.5, 0.5), 6, 0.2, "."
        ax.scatter(
            inner_skull_megt[0, 0:-1:20],
            inner_skull_megt[1, 0:-1:20],
            inner_skull_megt[2, 0:-1:20],
            color=color,
            marker=marker,
            s=scale,
            alpha=alpha,
        )

        # vol source grid points
        if src is not None and len(src_pnts.T) > 0:
            color, scale, alpha, marker = (1, 0, 0), 1, 0.5, "."
            src_pntst = src_pnts.T
            ax.scatter(
                src_pntst[0, :],
                src_pntst[1, :],
                src_pntst[2, :],
                color=color,
                marker=marker,
                s=scale,
                alpha=alpha,
            )

        if filename is None:
            plt.show()
        else:
            plt.savefig(filename)
            plt.close()
    else:
        raise ValueError("invalid plot_type")

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("ignore", Warning)


def setup_volume_source_space(
    subjects_dir, subject, gridstep=5, mindist=5.0, exclude=0.0, logger=None
):
    """Set up a volume source space grid inside the inner skull surface.
    This is a RHINO specific version of mne.setup_volume_source_space.

    Parameters
    ----------
    subjects_dir : string
        Directory to find RHINO subject dirs in.
    subject : string
        Subject name dir to find RHINO files in.
    gridstep : int
        A grid will be constructed with the spacing given by ``gridstep`` in mm,
        generating a volume source space.
    mindist : float
        Exclude points closer than this distance (mm) to the bounding surface.
    exclude : float
        Exclude points closer than this distance (mm) from the center of mass
        of the bounding surface.
    logger : logging.getLogger
        Logger

    Returns
    -------
    src : SourceSpaces
        A single source space object.

    See Also
    --------
    mne.setup_volume_source_space

    Notes
    -----
    This is a RHINO specific version of mne.setup_volume_source_space, which
    can handle smri's that are niftii files. This specifically
    uses the inner skull surface in:
        get_surfaces_filenames(subjects_dir, subject)['bet_inskull_surf_file']
    to define the source space grid.

    This will also copy the:
        get_surfaces_filenames(subjects_dir, subject)['bet_inskull_surf_file']
    file to:
        subjects_dir/subject/bem/inner_skull.surf
    since this is where mne expects to find it when mne.make_bem_model
    is called.

    The coords of points to reconstruct to can be found in the output here:
        src[0]['rr'][src[0]['vertno']]
    where they are in native MRI space in metres.
    """

    pos = int(gridstep)

    surfaces_filenames = get_surfaces_filenames(subjects_dir, subject)

    # -------------------------------------------------------------------------
    # Move the surfaces to where MNE expects to find them for the
    # forward modelling, see make_bem_model in mne/bem.py

    # First make sure bem directory exists:
    bem_dir_name = op.join(subjects_dir, subject, "bem")
    if not op.isdir(bem_dir_name):
        os.mkdir(bem_dir_name)

    # Note that due to the unusal naming conventions used by BET and MNE:
    # - bet_inskull_*_file is actually the brain surface
    # - bet_outskull_*_file is actually the inner skull surface
    # - bet_outskin_*_file is the outer skin/scalp surface
    # These correspond in mne to (in order):
    # - inner_skull
    # - outer_skull
    # - outer_skin
    #
    # This means that for single shell model, i.e. with conductivities set
    # to length one, the surface used by MNE willalways be the inner_skull, i.e.
    # it actually corresponds to the brain/cortex surface!! Not sure that is
    # correct/optimal.
    #
    # Note that this is done in Fieldtrip too!, see the
    # "Realistic single-shell model, using brain surface from segmented mri"
    # section at:
    # https://www.fieldtriptoolbox.org/example/make_leadfields_using_different_headmodels/#realistic-single-shell-model-using-brain-surface-from-segmented-mri
    #
    # However, others are clear that it should really be the actual inner surface
    # of the skull, see the "single-shell Boundary Element Model (BEM)" bit at:
    # https://imaging.mrc-cbu.cam.ac.uk/meg/SpmForwardModels
    #
    # To be continued... need to get in touch with mne folks perhaps?

    verts, tris = read_surface(surfaces_filenames["bet_inskull_surf_file"])
    tris = tris.astype(int)
    write_surface(
        op.join(bem_dir_name, "inner_skull.surf"),
        verts,
        tris,
        file_format="freesurfer",
        overwrite=True,
    )
    log_or_print("Using bet_inskull_surf_file for single shell surface", logger)

    #verts, tris = read_surface(surfaces_filenames["bet_outskull_surf_file"])
    #tris = tris.astype(int)
    #write_surface(
    #    op.join(bem_dir_name, "inner_skull.surf"),
    #    verts,
    #    tris,
    #    file_format="freesurfer",
    #    overwrite=True,
    #)
    #print("Using bet_outskull_surf_file for single shell surface")

    verts, tris = read_surface(surfaces_filenames["bet_outskull_surf_file"])
    tris = tris.astype(int)
    write_surface(
        op.join(bem_dir_name, "outer_skull.surf"),
        verts,
        tris,
        file_format="freesurfer",
        overwrite=True,
    )

    verts, tris = read_surface(surfaces_filenames["bet_outskin_surf_file"])
    tris = tris.astype(int)
    write_surface(
        op.join(bem_dir_name, "outer_skin.surf"),
        verts,
        tris,
        file_format="freesurfer",
        overwrite=True,
    )

    # -------------------------------------------------------------------------
    # Setup main MNE call to _make_volume_source_space

    surface = op.join(subjects_dir, subject, "bem", "inner_skull.surf")

    pos = float(pos)
    pos /= 1000.0  # convert pos to m from mm for MNE call

    # -------------------------------------------------------------------------
    def get_mri_info_from_nii(mri):
        out = dict()
        dims = nib.load(mri).get_fdata().shape
        out.update(
            mri_width=dims[0],
            mri_height=dims[1],
            mri_depth=dims[1],
            mri_volume_name=mri,
        )
        return out

    vol_info = get_mri_info_from_nii(surfaces_filenames["smri_file"])

    surf = read_surface(surface, return_dict=True)[-1]

    surf = deepcopy(surf)
    surf["rr"] *= 1e-3  # must be in metres for MNE call

    # Main MNE call to _make_volume_source_space
    sp = _make_volume_source_space(
        surf,
        pos,
        exclude,
        mindist,
        surfaces_filenames["smri_file"],
        None,
        vol_info=vol_info,
        single_volume=False,
    )

    sp[0]["type"] = "vol"

    # -------------------------------------------------------------------------
    # Save and return result

    sp = _complete_vol_src(sp, subject)

    # add dummy mri_ras_t and vox_mri_t transforms as these are needed for the
    # forward model to be saved (for some reason)
    sp[0]["mri_ras_t"] = Transform("mri", "ras")

    sp[0]["vox_mri_t"] = Transform("mri_voxel", "mri")

    if sp[0]["coord_frame"] != FIFF.FIFFV_COORD_MRI:
        raise RuntimeError("source space is not in MRI coordinates")

    return sp