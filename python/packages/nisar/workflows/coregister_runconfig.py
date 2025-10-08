#!/usr/bin/env python3
import os
import warnings

import journal
import nisar.workflows.helpers as helpers
from nisar.workflows.runconfig import RunConfig
from nisar.products.readers import SLC
from nisar.workflows.geo2rdr_runconfig import Geo2rdrRunConfig
from nisar.workflows.geocode_insar_runconfig import geocode_insar_cfg_check
from nisar.workflows.ionosphere_runconfig import ionosphere_cfg_check
from nisar.workflows.troposphere_runconfig import troposphere_delay_check


class CoregRunConfig(RunConfig):
    def __init__(self, args):
        super().__init__(args,'coreg')
        self.load_geocode_yaml_to_dict()
#        self.geocode_common_arg_load()
        self.prep_frequency_and_polarizations()
        self.yaml_check()

    def prep_frequency_and_polarizations(self):
        '''
        check frequency and polarizations and fix as needed
        '''
        error_channel = journal.error('RunConfig.prep_frequency_and_polarizations')
        if self.workflow_name == 'insar' or self.workflow_name == 'coreg':
            input_path = self.cfg['input_file_group']['reference_rslc_file']
        else:
            input_path = self.cfg['input_file_group']['input_file_path']
        freq_pols = self.cfg['processing']['input_subset']['list_of_frequencies']

        slc = SLC(hdf5file=input_path)

        # if freq_pols is empty, process all frequencies and polarizations
        if freq_pols is None:
            self.cfg['processing']['input_subset']['list_of_frequencies'] = \
                slc.polarizations
            return

        # otherwise, check contents of freq_pols
        for freq in freq_pols.keys():
            if freq not in slc.frequencies:
                err_str = (f'Requested frequency {freq} not found in input'
                           ' product.')
                error_channel.log(err_str)
                raise ValueError(err_str)

            # first check polarizations from source hdf5
            rslc_pols = slc.polarizations[freq]
            # use all RSLC polarizations if None provided
            if freq_pols[freq] is None:
                freq_pols[freq] = rslc_pols
                continue

            # use polarizations provided by user
            # check if user provided polarizations match RSLC ones
            for usr_pol in freq_pols[freq]:
                if usr_pol not in rslc_pols:
                    err_str = (f'Requested polarization {usr_pol}'
                               ' not found in input product.')
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

