#!/usr/bin/env python
# -*- coding: utf-8 -*-
import argparse
import os

from dipy.core.gradients import gradient_table
from dipy.io.gradients import read_bvals_bvecs
from dipy.reconst.dti import fractional_anisotropy, TensorModel
from fury import actor
import nibabel as nib
import numpy as np

from scilpy.io.utils import (add_overwrite_arg,
                             assert_inputs_exist,
                             assert_outputs_exist)
from scilpy.utils.bvec_bval_tools import normalize_bvecs, get_shell_indices
from scilpy.utils.image import register_image
from scilpy.viz.screenshot import display_slices

DESCRIPTION = """
Register DWI to a template for screenshots.
The templates are on http://www.bic.mni.mcgill.ca/ServicesAtlases/ICBM152NLin2009
For quick quality control, the MNI template can be downsampled to 2mm iso.
Axial, coronal and sagittal slices are captured.
"""


def _build_args_parser():
    p = argparse.ArgumentParser(
        description=DESCRIPTION,
        formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument('dwi',
                   help='Path of the input diffusion volume.')
    p.add_argument('bval',
                   help='Path of the bval file, in FSL format.')
    p.add_argument('bvec',
                   help='Path of the bvec file, in FSL format.')
    p.add_argument('target_template',
                   help='Path to the target MNI152 template for registration,\n'
                        'use the one provided online.')
    p.add_argument('--shells', type=int, nargs='+',
                   help='Shells to use for DTI fit (usually below 1200), '
                        'b0 must be listed.')
    p.add_argument('--output_suffix',
                   help='Add a suffix to the output, else the axis name is used.')
    p.add_argument('--output_dir', default='',
                   help='Put all images in a specific directory.')
    add_overwrite_arg(p)

    return p


def prepare_data_for_actors(dwi_filename, bvals_filename, bvecs_filename,
                            target_template_filename, slices_choice,
                            shells=None):
    # Load and prepare the data
    dwi_img = nib.load(dwi_filename)
    dwi_data = dwi_img.get_data()
    dwi_affine = dwi_img.get_affine()

    bvals, bvecs = read_bvals_bvecs(bvals_filename, bvecs_filename)

    target_template_img = nib.load(target_template_filename)
    target_template_data = target_template_img.get_data()
    target_template_affine = target_template_img.affine
    mask_data = np.zeros(target_template_data.shape)
    mask_data[target_template_data > 0] = 1

    # Prepare mask for tensors fit
    x_slice, y_slice, z_slice = slices_choice
    mask_data = prepare_slices_mask(mask_data,
                                    x_slice, y_slice, z_slice)

    # Extract B0
    gtab = gradient_table(bvals, normalize_bvecs(bvecs), b0_threshold=10)
    b0_idx = np.where(gtab.b0s_mask)[0]
    mean_b0 = np.mean(dwi_data[..., b0_idx], axis=3, dtype=dwi_data.dtype)

    if shells:
        indices = [get_shell_indices(bvals, shell) for shell in shells]
        indices = np.sort(np.hstack(indices))

        if len(indices) < 1:
            raise ValueError(
                'There are no volumes that have the supplied b-values.')
        shell_data = np.zeros((dwi_data.shape[:-1] + (len(indices),)),
                              dtype=dwi_data.dtype)
        shell_bvecs = np.zeros((len(indices), 3))
        shell_bvals = np.zeros((len(indices),))
        for i, indice in enumerate(indices):
            shell_data[..., i] = dwi_data[..., indice]
            shell_bvals[i] = bvals[indice]
            shell_bvecs[i, :] = bvecs[indice, :]
    else:
        shell_data = dwi_data
        shell_bvals = bvals
        shell_bvecs = bvecs

    # Register the DWI data to the template
    transformed_dwi, transformation = register_image(target_template_data,
                                                     target_template_affine,
                                                     mean_b0,
                                                     dwi_affine,
                                                     transformation_type='rigid',
                                                     dwi=shell_data)

    # Rotate gradients
    rotated_bvecs = np.dot(shell_bvecs, transformation[0:3, 0:3])

    rotated_bvecs = normalize_bvecs(rotated_bvecs)
    rotated_gtab = gradient_table(shell_bvals, rotated_bvecs, b0_threshold=10)

    # Get tensors
    tensor_model = TensorModel(rotated_gtab, fit_method='LS')
    tensor_fit = tensor_model.fit(transformed_dwi, mask_data)
    # Get FA
    fa_map = np.clip(fractional_anisotropy(tensor_fit.evals), 0, 1)

    # Get eigen vals/vecs
    evals = np.zeros(target_template_data.shape + (1,))
    evals[..., 0] = tensor_fit.evals[..., 0] / np.max(tensor_fit.evals[..., 0])
    evecs = np.zeros(target_template_data.shape + (1, 3))
    evecs[:, :, :, 0, :] = tensor_fit.evecs[..., 0]

    return fa_map, evals, evecs


def prepare_slices_mask(mask_data, x_slice, y_slice, z_slice):
    mask_slices = np.zeros(mask_data.shape)
    mask_slices[x_slice, :, :] += mask_data[x_slice, :, :]
    mask_slices[:, y_slice, :] += mask_data[:, y_slice, :]
    mask_slices[:, :, z_slice] += mask_data[:, :, z_slice]
    mask_slices[mask_slices > 1] = 1

    return mask_slices.astype(np.uint8)


def main():
    parser = _build_args_parser()
    args = parser.parse_args()
    required = [args.dwi, args.bval, args.bvec, args.target_template]
    assert_inputs_exist(parser, required)

    output_filenames = []
    for axis_name in ['sagittal', 'coronal', 'axial']:
        if args.output_suffix:
            output_filenames.append(os.path.join(args.output_dir,
                                                 '{0}_{1}.png'.format(
                                                     axis_name,
                                                     args.output_suffix)))
        else:
            output_filenames.append(os.path.join(args.output_dir,
                                                 '{0}.png'.format(axis_name)))

    assert_outputs_exist(parser, args, output_filenames)

    if args.output_dir and not os.path.isdir(args.output_dir):
        os.mkdir(args.output_dir)

    # Get the relevant slices from the template
    target_template_img = nib.load(args.target_template)
    zooms = 1 / float(target_template_img.header.get_zooms()[0])

    x_slice = int(target_template_img.shape[0] / 2 + zooms*30)
    y_slice = int(target_template_img.shape[1] / 2)
    z_slice = int(target_template_img.shape[2] / 2)
    slices_choice = (x_slice, y_slice, z_slice)

    FA, evals, evecs = prepare_data_for_actors(args.dwi, args.bval, args.bvec,
                                               args.target_template,
                                               slices_choice,
                                               shells=args.shells)

    # Create actors from each dataset for Dipy
    volume_actor = actor.slicer(FA, opacity=0.3, interpolation='nearest')
    peaks_actor = actor.peak_slicer(evecs, peaks_values=evals,
                                    colors=None, linewidth=1)

    # Take a snapshot of each dataset, camera setting are fixed for the
    # known template, won't work with another.
    display_slices(volume_actor, slices_choice,
                   output_filenames[0], 'sagittal',
                   view_position=tuple([zooms*x for x in (-50, 120, 100)]),
                   focal_point=tuple([zooms*x for x in (80, 120, 100)]),
                   peaks_actor=peaks_actor)
    display_slices(volume_actor, slices_choice,
                   output_filenames[1], 'coronal',
                   view_position=tuple([zooms*x for x in (95, 200, 100)]),
                   focal_point=tuple([zooms*x for x in (95, 100, 100)]),
                   peaks_actor=peaks_actor)
    display_slices(volume_actor, slices_choice,
                   output_filenames[2], 'axial',
                   view_position=tuple([zooms*x for x in (100, 100, -75)]),
                   focal_point=tuple([zooms*x for x in (100, 100, 90)]),
                   peaks_actor=peaks_actor)


if __name__ == "__main__":
    main()
