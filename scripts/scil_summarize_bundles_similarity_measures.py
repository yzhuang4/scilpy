#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import copy
import hashlib
import itertools
import json
import logging
import multiprocessing
import os
import shutil

from dipy.io.stateful_tractogram import Space, StatefulTractogram
from dipy.io.streamline import load_tractogram, save_tractogram
from dipy.io.utils import is_header_compatible, get_reference_info
from dipy.segment.clustering import qbx_and_merge
import nibabel as nib
import numpy as np
from numpy.random import RandomState

from scilpy.io.utils import (add_overwrite_arg,
                             add_reference_arg,
                             assert_inputs_exist,
                             assert_outputs_exist,
                             link_bundles_and_reference)
from scilpy.tractanalysis.reproducibility_measures \
    import (compute_dice_voxel,
            compute_bundle_adjacency_streamlines,
            compute_bundle_adjacency_voxel,
            compute_dice_streamlines,
            get_endpoints_density_map)
from scilpy.tractanalysis.streamlines_metrics import compute_tract_counts_map


DESCRIPTION = """
Compute pair-wise similarity measures of bundles.
All tractograms must be in the same space (aligned to one reference)
"""


def _build_args_parser():
    p = argparse.ArgumentParser(
        description=DESCRIPTION,
        formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument('in_bundles', nargs='+',
                   help='Path of the input bundles.')
    p.add_argument('out_json',
                   help='Path of the output json file.')

    p.add_argument('--streamline_dice', action='store_true',
                   help='Streamlines-wise Dice coefficient will be computed \n'
                        'Tractograms must be identical [%(default)s].')
    p.add_argument('--disable_streamline_distance', action='store_true',
                   help='Will not compute the streamlines distance \n'
                        '[%(default)s].')
    p.add_argument('--single_compare',
                   help='Compare inputs to this single file.')
    p.add_argument('--processes', type=int,
                   help='Number of processes to use [ALL].')
    p.add_argument('--keep_tmp', action='store_true',
                   help='Will not delete the tmp folder at the end.')

    add_reference_arg(p)
    add_overwrite_arg(p)

    return p


def load_data_tmp_saving_wrapper(args):
    load_data_tmp_saving(args[0][0], args[0][1],
                         init_only=args[1],
                         disable_centroids=args[2])


def load_data_tmp_saving(filename, reference, init_only=False,
                         disable_centroids=False):
    # Since data is often re-use when comparing multiple bundles, anything
    # that can be computed once is saved temporarily and simply loaded on demand
    if not os.path.isfile(filename):
        if init_only:
            logging.warning('%s does not exist', filename)
        return None

    hash_tmp = hashlib.md5(filename.encode()).hexdigest()
    tmp_density_filename = os.path.join('tmp_measures/',
                                        '{0}_density.nii.gz'.format(hash_tmp))
    tmp_endpoints_filename = os.path.join('tmp_measures/',
                                          '{0}_endpoints.nii.gz'.format(hash_tmp))
    tmp_centroids_filename = os.path.join('tmp_measures/',
                                          '{0}_centroids.trk'.format(hash_tmp))

    sft = load_tractogram(filename, reference,
                          to_space=Space.VOX,
                          shifted_origin=True)
    streamlines = sft.get_streamlines_copy()
    if not streamlines:
        if init_only:
            logging.warning('%s is empty', filename)
        return None

    if os.path.isfile(tmp_density_filename) \
            and os.path.isfile(tmp_endpoints_filename) \
            and os.path.isfile(tmp_centroids_filename):
        # If initilization, loading the data is useless
        if init_only:
            return None
        density = nib.load(tmp_density_filename).get_data()
        endpoints_density = nib.load(tmp_endpoints_filename).get_data()
        sft_centroids = load_tractogram(tmp_centroids_filename, reference,
                                        to_space=Space.VOX,
                                        shifted_origin=True)
        centroids = sft_centroids.get_streamlines_copy()
    else:
        transformation, dimensions, _, _ = sft.space_attribute
        density = compute_tract_counts_map(streamlines, dimensions)
        endpoints_density = get_endpoints_density_map(streamlines, dimensions,
                                                      point_to_select=3)
        thresholds = [32, 24, 12, 6]
        if disable_centroids:
            centroids = []
        else:
            centroids = qbx_and_merge(streamlines, thresholds,
                                      rng=RandomState(0),
                                      verbose=False).centroids

        # Saving tmp files to save on future computation
        nib.save(nib.Nifti1Image(density.astype(np.float32), transformation),
                 tmp_density_filename)
        nib.save(nib.Nifti1Image(endpoints_density.astype(np.int16),
                                 transformation),
                 tmp_endpoints_filename)

        centroids_sft = StatefulTractogram(centroids, reference, Space.VOX,
                                           shifted_origin=True)
        save_tractogram(centroids_sft, tmp_centroids_filename)

    return density, endpoints_density, streamlines, centroids


def compute_all_measures(args):
    tuple_1, tuple_2 = args[0]
    filename_1, reference_1 = tuple_1
    filename_2, reference_2 = tuple_2
    streamline_dice = args[1]
    disable_streamline_distance = args[2]

    if not is_header_compatible(reference_1, reference_2):
        raise ValueError('{0} and {1} have incompatible headers'.format(
            filename_1, filename_2))

    data_tuple_1 = load_data_tmp_saving(
        filename_1, reference_1,
        disable_centroids=disable_streamline_distance)
    if data_tuple_1 is None:
        return None

    density_1, endpoints_density_1, bundle_1, \
        centroids_1 = data_tuple_1

    data_tuple_2 = load_data_tmp_saving(
        filename_2, reference_2,
        disable_centroids=disable_streamline_distance)
    if data_tuple_2 is None:
        return None

    density_2, endpoints_density_2, bundle_2, \
        centroids_2 = data_tuple_2

    _, _, voxel_size, _ = get_reference_info(reference_1)
    voxel_size = np.product(voxel_size)

    # These measures are in mm^3
    binary_1 = copy.copy(density_1)
    binary_1[binary_1 > 0] = 1
    binary_2 = copy.copy(density_2)
    binary_2[binary_2 > 0] = 1
    volume_overlap = np.count_nonzero(binary_1 * binary_2)
    volume_overlap_endpoints = np.count_nonzero(
        endpoints_density_1 * endpoints_density_2)
    volume_overreach = np.abs(np.count_nonzero(
        binary_1 + binary_2) - volume_overlap)
    volume_overreach_endpoints = np.abs(np.count_nonzero(
        endpoints_density_1 + endpoints_density_2) - volume_overlap_endpoints)

    # These measures are in mm
    bundle_adjacency_voxel = compute_bundle_adjacency_voxel(density_1,
                                                            density_2,
                                                            non_overlap=True)
    if streamline_dice and not disable_streamline_distance:
        bundle_adjacency_streamlines = \
            compute_bundle_adjacency_streamlines(bundle_1,
                                                 bundle_2,
                                                 non_overlap=True)
    elif not disable_streamline_distance:
        bundle_adjacency_streamlines = \
            compute_bundle_adjacency_streamlines(bundle_1,
                                                 bundle_2,
                                                 centroids_1=centroids_1,
                                                 centroids_2=centroids_2,
                                                 non_overlap=True)
    # These measures are between 0 and 1
    dice_vox, w_dice_vox = compute_dice_voxel(density_1,
                                              density_2)
    indices = np.where(density_1 + density_2 > 0)
    indices_endpoints = np.where(endpoints_density_1 + endpoints_density_2 > 0)
    dice_vox_endpoints, w_dice_vox_endpoints = compute_dice_voxel(
        endpoints_density_1,
        endpoints_density_2)
    density_correlation = np.corrcoef(
        density_1[indices], density_2[indices])[0, 1]
    corrcoef = np.corrcoef(endpoints_density_1[indices_endpoints],
                           endpoints_density_2[indices_endpoints])
    density_correlation_endpoints = corrcoef[0, 1]

    measures_name = ['bundle_adjacency_voxels',
                     'dice_voxels', 'w_dice_voxels',
                     'volume_overlap',
                     'volume_overreach',
                     'dice_voxels_endpoints',
                     'w_dice_voxels_endpoints',
                     'volume_overlap_endpoints',
                     'volume_overreach_endpoints',
                     'density_correlation',
                     'density_correlation_endpoints']
    measures = [bundle_adjacency_voxel,
                dice_vox, w_dice_vox,
                volume_overlap * voxel_size,
                volume_overreach * voxel_size,
                dice_vox_endpoints,
                w_dice_vox_endpoints,
                volume_overlap_endpoints * voxel_size,
                volume_overreach_endpoints * voxel_size,
                density_correlation,
                density_correlation_endpoints]

    if not disable_streamline_distance:
        measures_name += ['bundle_adjacency_streamlines']
        measures += [bundle_adjacency_streamlines]

    # Only when the tractograms are exactly the same
    if streamline_dice:
        dice_streamlines, streamlines_intersect, streamlines_union = \
            compute_dice_streamlines(bundle_1, bundle_2)
        streamlines_count_overlap = len(streamlines_intersect)
        streamlines_count_overreach = len(
            streamlines_union) - len(streamlines_intersect)
        measures_name += ['dice_streamlines',
                          'streamlines_count_overlap',
                          'streamlines_count_overreach']
        measures += [dice_streamlines,
                     streamlines_count_overlap,
                     streamlines_count_overreach]

    return dict(zip(measures_name, measures))


def main():
    parser = _build_args_parser()
    args = parser.parse_args()

    assert_inputs_exist(parser, args.in_bundles)
    assert_outputs_exist(parser, args, [args.out_json])

    nbr_cpu = args.processes if args.processes else multiprocessing.cpu_count()
    if nbr_cpu <= 0:
        parser.error('Number of processes cannot be <= 0.')
    elif nbr_cpu > multiprocessing.cpu_count():
        parser.error('Max number of processes is {}. Got {}.'.format(
            multiprocessing.cpu_count(), nbr_cpu))

    if not os.path.isdir('tmp_measures/'):
        os.mkdir('tmp_measures/')

    pool = multiprocessing.Pool(nbr_cpu)

    if args.single_compare:
        # Move the single_compare only once, at the end.
        if args.single_compare in args.in_bundles:
            args.in_bundles.remove(args.single_compare)
        bundles_list = args.in_bundles + [args.single_compare]
        bundles_references_tuple_extended = link_bundles_and_reference(
            parser, args, bundles_list)

        single_compare_reference_tuple = bundles_references_tuple_extended.pop()
        comb_dict_keys = list(itertools.product(bundles_references_tuple_extended,
                                                [single_compare_reference_tuple]))
    else:
        bundles_list = args.in_bundles
        # Pre-compute the needed files, to avoid conflict when the number
        # of cpu is higher than the number of bundle
        bundles_references_tuple = link_bundles_and_reference(parser,
                                                              args,
                                                              bundles_list)
        pool.map(load_data_tmp_saving_wrapper,
                 zip(bundles_references_tuple,
                     itertools.repeat(True),
                     itertools.repeat(args.disable_streamline_distance)))

        comb_dict_keys = list(itertools.combinations(
            bundles_references_tuple, r=2))

    all_measures_dict = pool.map(compute_all_measures,
                                 zip(comb_dict_keys,
                                     itertools.repeat(args.streamline_dice),
                                     itertools.repeat(args.disable_streamline_distance)))
    pool.close()
    pool.join()

    output_measures_dict = {}
    for measure_dict in all_measures_dict:
        # Empty bundle should not make the script crash
        if measure_dict is not None:
            for measure_name in measure_dict.keys():
                # Create an empty list first
                if measure_name not in output_measures_dict:
                    output_measures_dict[measure_name] = []
                output_measures_dict[measure_name].append(
                    measure_dict[measure_name])

    with open(args.out_json, 'w') as outfile:
        json.dump(output_measures_dict, outfile)

    if not args.keep_tmp:
        shutil.rmtree('tmp_measures/')


if __name__ == "__main__":
    main()
