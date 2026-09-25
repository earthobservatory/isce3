#!/usr/bin/env python3
import os
import warnings

import journal
import numpy as np
import nisar.workflows.helpers as helpers
from nisar.workflows.runconfig import RunConfig
from nisar.products.readers import SLC
from nisar.products.readers.orbit import load_orbit_from_xml
from isce3.splitspectrum import splitspectrum
from nisar.workflows.geo2rdr_runconfig import Geo2rdrRunConfig
from nisar.workflows.geocode_insar_runconfig import geocode_insar_cfg_check
from nisar.workflows.ionosphere_runconfig import ionosphere_cfg_check
from nisar.workflows.troposphere_runconfig import troposphere_delay_check


class CoregRunConfig(RunConfig):
    # Relative tolerance for comparing reference and secondary radar grid
    # parameters. Acquisitions of the same mode can differ at the sub-ppm
    # level, so an exact comparison would flag every pair.
    GRID_RTOL = 1e-6

    def __init__(self, args):
        super().__init__(args,'coreg')
        self.load_geocode_yaml_to_dict()
        # RunConfig.geocode_common_arg_load() cannot be called for this
        # workflow: its prep_paths() raises ValueError('coreg unsupported')
        # and its check_temporal_coverage() assumes the gcov/gslc
        # 'input_file_path' key layout. The methods below do the equivalent
        # validation with the same helpers, the coreg-specific difference
        # being that the secondary RSLC is optional.
        self.check_input_paths()
        self.prep_frequency_and_polarizations()
        self.check_rslc_compatibility()
        self.check_orbit_coverage()
        self.yaml_check()

    def check_input_paths(self):
        '''
        Validate input RSLCs, external orbits, DEM and output directories.

        Stands in for RunConfig.prep_paths(). Unlike the base version the
        secondary RSLC and its orbit are optional, because a reference-only
        pass (target_file_type: reference) leaves them blank.
        '''
        error_channel = journal.error('CoregRunConfig.check_input_paths')

        input_group = self.cfg['input_file_group']
        orbit_group = \
            self.cfg['dynamic_ancillary_file_group'].get('orbit_files') or {}

        rslc_keys = ['reference_rslc_file']
        if input_group.get('secondary_rslc_file'):
            rslc_keys.append('secondary_rslc_file')

        for rslc_key in rslc_keys:
            rslc_path = input_group[rslc_key]
            if not rslc_path or not os.path.isfile(rslc_path):
                err_str = f'{rslc_key} "{rslc_path}" RSLC not found'
                error_channel.log(err_str)
                raise ValueError(err_str)

        # External orbits are optional, but must exist when named. Without
        # this check a mistyped path silently falls back to the orbit
        # embedded in the RSLC.
        for orbit_key in ['reference_orbit_file', 'secondary_orbit_file']:
            orbit_path = orbit_group.get(orbit_key)
            if orbit_path is not None and not os.path.isfile(orbit_path):
                err_str = f'External orbit file "{orbit_path}" is not valid'
                error_channel.log(err_str)
                raise FileNotFoundError(err_str)

        helpers.check_dem(
            self.cfg['dynamic_ancillary_file_group']['dem_file'])

        output_hdf5 = self.cfg['product_path_group']['sas_output_file']
        helpers.check_write_dir(os.path.dirname(output_hdf5))
        helpers.check_write_dir(self.cfg['product_path_group']['scratch_path'])

    def check_orbit_coverage(self):
        '''
        Check that each orbit spans the sensing window of its RSLC.

        Stands in for RunConfig.check_temporal_coverage(). It checks BOTH
        orbits rather than only the reference: geo2rdr is driven by the
        secondary orbit, so that is the one whose coverage determines
        coregistration. An external orbit that does not span the acquisition
        is otherwise cropped silently by crop_external_orbit() in rdr2geo.py
        and geo2rdr.py.
        '''
        info_channel = journal.info('CoregRunConfig.check_orbit_coverage')

        input_group = self.cfg['input_file_group']
        orbit_group = \
            self.cfg['dynamic_ancillary_file_group'].get('orbit_files') or {}
        tec_path = self.cfg['dynamic_ancillary_file_group']['tec_file']
        freq_pols = \
            self.cfg['processing']['input_subset']['list_of_frequencies']

        targets = [('reference', input_group['reference_rslc_file'],
                    orbit_group.get('reference_orbit_file'))]
        if input_group.get('secondary_rslc_file'):
            targets.append(('secondary', input_group['secondary_rslc_file'],
                            orbit_group.get('secondary_orbit_file')))

        for label, rslc_path, orbit_path in targets:
            slc = SLC(hdf5file=rslc_path)
            if orbit_path is not None:
                orbit = load_orbit_from_xml(orbit_path,
                                            slc.getRadarGrid().ref_epoch)
                info_channel.log(
                    f'{label} RSLC: using external orbit {orbit_path}')
            else:
                orbit = slc.getOrbit()
                info_channel.log(
                    f'{label} RSLC: no external orbit given, using the orbit '
                    'embedded in the product')
            for freq in freq_pols:
                helpers.check_radargrid_orbit_tec(slc.getRadarGrid(freq),
                                                  orbit, tec_path)

    def check_rslc_compatibility(self):
        '''
        Check that the reference and secondary RSLCs can be coregistered, and
        that any bandpassing they need is one this workflow can perform.

        prep_frequency_and_polarizations() intersects frequencies and
        polarizations; nothing else compares acquisition geometry or the range
        spectrum.

        When the two products differ in wavelength or range bandwidth,
        coregister.py runs bandpass_insar, whose
        splitspectrum.check_range_bandwidth_overlap() bandpasses whichever
        product has the WIDER bandwidth down to the band of the narrower one.
        The coreg workflow computes rdr2geo once for the reference and reuses
        that topo.vrt for every secondary, so the reference grid must never
        change -- which means the reference must never be the bandpass target.
        The guards below enforce that, and the two preconditions the stock
        bandpass code assumes but does not check.
        '''
        error_channel = journal.error('CoregRunConfig.check_rslc_compatibility')
        warning_channel = \
            journal.warning('CoregRunConfig.check_rslc_compatibility')
        info_channel = journal.info('CoregRunConfig.check_rslc_compatibility')

        sec_path = self.cfg['input_file_group'].get('secondary_rslc_file')
        if not sec_path:
            return

        ref_slc = SLC(
            hdf5file=self.cfg['input_file_group']['reference_rslc_file'])
        sec_slc = SLC(hdf5file=sec_path)
        freq_pols = \
            self.cfg['processing']['input_subset']['list_of_frequencies']

        for freq in freq_pols:
            ref_grid = ref_slc.getRadarGrid(freq)
            sec_grid = sec_slc.getRadarGrid(freq)

            if ref_grid.lookside != sec_grid.lookside:
                err_str = (f'Frequency {freq}: reference look side '
                           f'{ref_grid.lookside} does not match secondary '
                           f'{sec_grid.lookside}; these products cannot be '
                           'coregistered.')
                error_channel.log(err_str)
                raise ValueError(err_str)

            if not np.isclose(ref_grid.prf, sec_grid.prf,
                              rtol=self.GRID_RTOL, atol=0.0):
                warning_channel.log(
                    f'Frequency {freq}: PRF differs between reference '
                    f'({ref_grid.prf}) and secondary ({sec_grid.prf}).')

            # Range spectrum. Uses the same metadata loader as bandpass_insar,
            # so the values checked here are the ones that drive bandpassing.
            ref_meta = splitspectrum.BandpassMetaData.load_from_slc(
                ref_slc, freq)
            sec_meta = splitspectrum.BandpassMetaData.load_from_slc(
                sec_slc, freq)

            same_wvl = np.isclose(ref_meta.wavelength, sec_meta.wavelength,
                                  rtol=self.GRID_RTOL, atol=0.0)
            same_bw = np.isclose(ref_meta.rg_bandwidth, sec_meta.rg_bandwidth,
                                 rtol=self.GRID_RTOL, atol=0.0)
            if same_wvl and same_bw:
                # check_range_bandwidth_overlap() returns {} -> bandpass is a
                # no-op for this pair. Nothing further to verify.
                continue

            self._check_bandpass_feasible(freq, ref_meta, sec_meta,
                                          error_channel, info_channel)

    def _check_bandpass_feasible(self, freq, ref_meta, sec_meta,
                                 error_channel, info_channel):
        '''
        Verify that bandpassing this pair is something the stock
        bandpass_insar/splitspectrum code can do correctly.

        Parameters
        ----------
        freq : str
            Frequency band ('A' or 'B') being checked.
        ref_meta, sec_meta : splitspectrum.BandpassMetaData
            Range-spectrum metadata of the reference and secondary.
        error_channel, info_channel : journal channels
            Channels used to report failures and the accepted plan.
        '''
        # Guard 1: the reference must never be the bandpass target, otherwise
        # its range spacing and slant range change and the shared rdr2geo
        # topo.vrt no longer describes it.
        if ref_meta.rg_bandwidth > sec_meta.rg_bandwidth * (1 + self.GRID_RTOL):
            err_str = (
                f'Frequency {freq}: reference range bandwidth '
                f'{ref_meta.rg_bandwidth / 1e6:.3f} MHz is wider than the '
                f'secondary {sec_meta.rg_bandwidth / 1e6:.3f} MHz, so '
                'bandpass would resample the REFERENCE. That would invalidate '
                'the shared rdr2geo topo.vrt used by every secondary. Choose a '
                'narrowest-bandwidth acquisition as the stack reference.')
            error_channel.log(err_str)
            raise ValueError(err_str)

        # base = narrower (reference), target = wider (secondary).
        base, target = ref_meta, sec_meta

        # Guard 2: bandpass_insar requests the base's FULL band from the
        # target, so that band must lie inside the target's own band.
        # Otherwise splitspectrum zero-fills the missing part while the
        # metadata still claims the full bandwidth.
        base_low = base.center_freq - 0.5 * base.rg_bandwidth
        base_high = base.center_freq + 0.5 * base.rg_bandwidth
        tgt_low = target.center_freq - 0.5 * target.rg_bandwidth
        tgt_high = target.center_freq + 0.5 * target.rg_bandwidth
        tol = self.GRID_RTOL * max(abs(tgt_low), abs(tgt_high))

        if base_low < tgt_low - tol or base_high > tgt_high + tol:
            err_str = (
                f'Frequency {freq}: the reference band '
                f'[{base_low / 1e6:.3f}, {base_high / 1e6:.3f}] MHz is not '
                f'contained in the secondary band '
                f'[{tgt_low / 1e6:.3f}, {tgt_high / 1e6:.3f}] MHz. '
                'bandpass_insar would request frequencies the secondary does '
                'not contain; splitspectrum fills those silently with zeros '
                'while reporting the full bandwidth. Coregistering this pair '
                'requires bandpassing both products to the overlapping band, '
                'which this workflow does not implement.')
            error_channel.log(err_str)
            raise ValueError(err_str)

        # Guard 3: splitspectrum decimates the target onto the base's sampling
        # grid and requires an integral decimation factor
        # (splitspectrum.bandpass_shift_spectrum raises otherwise).
        if base.rg_sample_freq <= 0:
            err_str = (f'Frequency {freq}: reference range sampling frequency '
                       f'{base.rg_sample_freq} is not usable.')
            error_channel.log(err_str)
            raise ValueError(err_str)

        factor = target.rg_sample_freq / base.rg_sample_freq
        if not np.isclose(factor, round(factor), rtol=0.0, atol=1e-7):
            err_str = (
                f'Frequency {freq}: bandpass decimation factor '
                f'{factor} is not an integer (secondary sampling frequency '
                f'{target.rg_sample_freq / 1e6:.4f} MHz / reference '
                f'{base.rg_sample_freq / 1e6:.4f} MHz). '
                'splitspectrum.bandpass_shift_spectrum requires an integral '
                'factor and would abort.')
            error_channel.log(err_str)
            raise ValueError(err_str)

        info_channel.log(
            f'Frequency {freq}: secondary will be bandpassed from '
            f'{target.rg_bandwidth / 1e6:.3f} MHz to the reference band '
            f'{base.rg_bandwidth / 1e6:.3f} MHz centred at '
            f'{base.center_freq / 1e6:.3f} MHz (decimation factor '
            f'{int(round(factor))}).')

    def prep_frequency_and_polarizations(self):
        '''
        check frequency and polarizations and fix as needed

        For the coreg/insar workflows, frequencies and polarizations are
        restricted to those common to both the reference and (if provided)
        the secondary RSLC, so that only frequency/polarization pairs that
        actually exist in both products get processed.
        '''
        error_channel = journal.error('RunConfig.prep_frequency_and_polarizations')
        if self.workflow_name == 'insar' or self.workflow_name == 'coreg':
            input_path = self.cfg['input_file_group']['reference_rslc_file']
        else:
            input_path = self.cfg['input_file_group']['input_file_path']
        freq_pols = self.cfg['processing']['input_subset']['list_of_frequencies']

        slc = SLC(hdf5file=input_path)

        sec_slc = None
        if self.workflow_name == 'coreg':
            sec_path = self.cfg['input_file_group'].get('secondary_rslc_file')
            if sec_path:
                sec_slc = SLC(hdf5file=sec_path)

        # frequencies common to reference (and secondary, if present)
        common_freqs = set(slc.frequencies)
        if sec_slc is not None:
            common_freqs &= set(sec_slc.frequencies)

        def common_pols(freq):
            '''polarizations common to reference (and secondary, if present) for freq'''
            pols = set(slc.polarizations[freq])
            if sec_slc is not None:
                pols &= set(sec_slc.polarizations[freq])
            return pols

        # if freq_pols is empty, process all common frequencies and polarizations
        if freq_pols is None:
            list_of_frequencies = {}
            for freq in common_freqs:
                pols = common_pols(freq)
                if not pols:
                    err_str = (f'No common polarization for frequency {freq}'
                               ' between reference and secondary RSLC.')
                    error_channel.log(err_str)
                    raise ValueError(err_str)
                list_of_frequencies[freq] = sorted(pols)
            self.cfg['processing']['input_subset']['list_of_frequencies'] = \
                list_of_frequencies
            return

        # otherwise, check contents of freq_pols
        for freq in freq_pols.keys():
            if freq not in slc.frequencies:
                err_str = (f'Requested frequency {freq} not found in reference'
                           ' RSLC product.')
                error_channel.log(err_str)
                raise ValueError(err_str)
            if sec_slc is not None and freq not in sec_slc.frequencies:
                err_str = (f'Requested frequency {freq} not found in secondary'
                           ' RSLC product.')
                error_channel.log(err_str)
                raise ValueError(err_str)

            # polarizations common to reference and secondary hdf5s
            rslc_pols = common_pols(freq)
            # use all common RSLC polarizations if None provided
            if freq_pols[freq] is None:
                if not rslc_pols:
                    err_str = (f'No common polarization for frequency {freq}'
                               ' between reference and secondary RSLC.')
                    error_channel.log(err_str)
                    raise ValueError(err_str)
                freq_pols[freq] = sorted(rslc_pols)
                continue

            # use polarizations provided by user
            # check if user provided polarizations match reference and
            # (if present) secondary RSLC ones
            for usr_pol in freq_pols[freq]:
                if usr_pol not in slc.polarizations[freq]:
                    err_str = (f'Requested polarization {usr_pol}'
                               ' not found in reference RSLC product.')
                    error_channel.log(err_str)
                    raise ValueError(err_str)
                if sec_slc is not None and usr_pol not in sec_slc.polarizations[freq]:
                    err_str = (f'Requested polarization {usr_pol}'
                               ' not found in secondary RSLC product.')
                    error_channel.log(err_str)
                    raise ValueError(err_str)

    def yaml_check(self):
        '''
        Check submodule paths from YAML
        '''

        scratch_path = self.cfg['product_path_group']['scratch_path']
        error_channel = journal.error('CoregRunConfig.yaml_check')
        warning_channel = journal.warning('CoregRunConfig.yaml_check')
        info_channel = journal.info('CoregRunConfig.yaml_check')

        # Extract frequencies and polarizations to process
        freq_pols = self.cfg['processing']['input_subset']['list_of_frequencies']
        frequencies = freq_pols.keys()
        topo_path = self.cfg['processing']['geo2rdr']['topo_path']
        if self.cfg['input_file_group']['secondary_rslc_file']:
            # check reference topo path exists if its a secondary RSLC to be coregistered
            helpers.check_mode_directory_tree(topo_path, 'rdr2geo', frequencies)

        # Check if rdr2geo flags enabled for topo X, Y, and Z rasters
        for xyz in 'xyz':
            # Get write flag for x, y, or z
            write_flag = f'write_{xyz}'

            # Check if it's not enabled (required for InSAR processing)
            if not self.cfg['processing']['rdr2geo'][write_flag]:
                # Raise and log warning
                warning_str = f'{write_flag} incorrectly disabled for rdr2geo; it will be enabled'
                warning_channel.log(warning_str)
                warnings.warn(warning_str)

                # Set write flag True
                self.cfg['processing']['rdr2geo'][write_flag] = True

        # for each submodule check if user path for input data assigned
        # if not assigned, assume it'll be in scratch
        if 'topo_path' not in self.cfg['processing']['geo2rdr']:
            self.cfg['processing']['geo2rdr']['topo_path'] = scratch_path

        if self.cfg['processing']['coarse_resample']['offsets_dir'] is None:
            self.cfg['processing']['coarse_resample']['offsets_dir'] = scratch_path

        # Bandpass window validation, ported from
        # BandpassRunConfig.yaml_check(). The schema enum only constrains keys
        # the user supplies, so a bad value coming from the defaults file would
        # otherwise reach splitspectrum unchecked.
        window_type = self.cfg['processing']['bandpass']['window_function']
        if window_type.lower() not in ['kaiser', 'cosine', 'tukey']:
            err_str = f"{window_type} not a valid window type"
            error_channel.log(err_str)
            raise ValueError(err_str)


        # If either dense_offsets and offsets_product are enabled and process
        # single co-pol for offsets enabled, check if co-pol values exist
        # Check a layer of offset exists

        # When running insar.py dense_offsets_path and geo2rdr_offsets_path
        # come from previous step through scratch_path

#        if 'coregistered_slc_path' not in self.cfg['processing']['crossmul']:
#            self.cfg['processing']['crossmul'][
#                'coregistered_slc_path'] = scratch_path

 #       flatten = self.cfg['processing']['crossmul']['flatten']
 #       if flatten:
 #           self.cfg['processing']['crossmul']['flatten_path'] = scratch_path

        # Check dictionary for interferogram filtering

        # If general mask is provided, check its existence
        #if 'general' in mask_options and mask_options['general'] is not None:
        #    if not os.path.isfile(mask_options['general']):
        #        err_str = f"The mask file {mask_options['general']} is not a file"
        #        error_channel.log(err_str)
        #        raise ValueError(err_str)
        #else:
        #    # Otherwise check that mask for individual freq/pols are correctly assigned
        #    for freq, pol_list in freq_pols.items():
        #        if freq in mask_options:
        #            for pol in pol_list:
        #                if pol in mask_options[freq]:
        #                    mask_file = mask_options[freq][pol]
        #                    if mask_file is not None and not os.path.isfile(mask_file):
        #                        err_str = f"{mask_file} is invalid; needs to be a file"
        #                        error_channel.log(err_str)
        #                        raise ValueError(err_str)

#        mode_type = self.cfg['processing']['baseline']['mode']
#        if mode_type.lower() not in ['3d_full', 'top_bottom']:
#            err_str = f"{mode_type} not a valid baseline estimation mode"
#            error_channel.log(err_str)
#            raise ValueError(err_str)

        # Check geocode_insar config options
#        geocode_insar_cfg_check(self.cfg)

