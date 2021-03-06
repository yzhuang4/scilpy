#!/usr/bin/env python
# -*- coding: utf-8 -*-
import argparse
import logging
import os

import nibabel as nib
from nibabel.streamlines import ArraySequence
import numpy as np

from scilpy.io.image import assert_same_resolution
from scilpy.io.streamlines import load_tractogram_with_reference
from scilpy.io.utils import (add_overwrite_arg,
                             assert_inputs_exist,
                             assert_output_dirs_exist_and_empty,
                             add_reference_arg)
from scilpy.utils.filenames import split_name_with_nii
from scilpy.tractanalysis.compute_tract_counts_map import \
     compute_tract_counts_map
from scilpy.tractanalysis.uncompress import uncompress


def _build_arg_parser():
    p = argparse.ArgumentParser(
        description='Projects metrics onto the endpoints of streamlines. The '
                    'idea is to visualize the cortical areas affected by '
                    'metrics (assuming streamlines start/end in the cortex).',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    p.add_argument('in_bundle',
                   help='Fiber bundle file.')
    p.add_argument('metrics',
                   nargs='+',
                   help='Nifti metric(s) to compute statistics on.')
    p.add_argument('output_folder',
                   help='Folder where to save endpoints metric.')

    add_reference_arg(p)
    add_overwrite_arg(p)
    return p


def _compute_streamline_mean(cur_ind, cur_min, cur_max, data):
    # From the precomputed indices, compute the binary map
    # and use it to weight the metric data for this specific streamline.
    cur_range = tuple(cur_max - cur_min)
    streamline_density = compute_tract_counts_map(ArraySequence([cur_ind]),
                                                  cur_range)
    streamline_data = data[cur_min[0]:cur_max[0],
                           cur_min[1]:cur_max[1],
                           cur_min[2]:cur_max[2]]
    streamline_average = np.average(streamline_data,
                                    weights=streamline_density)
    return streamline_average


def _process_streamlines(streamlines):
    # Compute the bounding boxes and indices for all streamlines.
    mins = []
    maxs = []
    offset_streamlines = []

    # Offset the streamlines to compute the indices only in the bounding box.
    # Reduces memory use later on.
    for idx, s in enumerate(streamlines):
        mins.append(np.min(s.astype(int), 0))
        maxs.append(np.max(s.astype(int), 0) + 1)
        offset_streamlines.append((s - mins[-1]).astype(np.float32))

    offset_streamlines = ArraySequence(offset_streamlines)
    indices = uncompress(offset_streamlines)

    return mins, maxs, indices


def main():
    parser = _build_arg_parser()
    args = parser.parse_args()

    assert_inputs_exist(parser, [args.in_bundle, args.metrics])
    assert_outputs_dir_exists_and_empty(parser, args, args.output_folder)

    metrics = [nib.load(metric) for metric in args.metrics]
    assert_same_resolution(*metrics)

    sft = load_tractogram_with_reference(parser, args, args.in_bundle)
    sft.to_vox()

    if len(sft.streamlines) == 0:
        logging.warning('Empty bundle file {}. Skipping'.format(args.bundle))
        return

    mins, maxs, indices = _process_streamlines(sft.streamlines)

    for metric in metrics:
        data = metric.get_data()
        endpoint_metric_map = np.zeros(metric.shape)
        count = np.zeros(metric.shape)
        for cur_min, cur_max, cur_ind, orig_s in zip(mins, maxs,
                                                     indices,
                                                     sft.streamlines):
            streamline_mean = _compute_streamline_mean(cur_ind,
                                                       cur_min,
                                                       cur_max,
                                                       data)

            xyz = orig_s[0, :].astype(int)
            endpoint_metric_map[xyz[0], xyz[1], xyz[2]] += streamline_mean
            count[xyz[0], xyz[1], xyz[2]] += 1

            xyz = orig_s[-1, :].astype(int)
            endpoint_metric_map[xyz[0], xyz[1], xyz[2]] += streamline_mean
            count[xyz[0], xyz[1], xyz[2]] += 1

        endpoint_metric_map[count != 0] /= count[count != 0]
        metric_fname, ext = split_name_with_nii(
            os.path.basename(metric.get_filename()))
        nib.save(nib.Nifti1Image(endpoint_metric_map, metric.affine,
                                 metric.header),
                 os.path.join(args.output_folder,
                              '{}_endpoints_metric{}'.format(metric_fname,
                                                             ext)))


if __name__ == '__main__':
    main()
