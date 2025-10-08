#!/usr/bin/env python3
import time
import os
import journal
from ruamel.yaml import YAML
from nisar.workflows import resample_slc_v2, geo2rdr, rdr2geo
from nisar.workflows.geocode_insar import InputProduct
from nisar.workflows.coregister_runconfig import CoregRunConfig
#from nisar.workflows.persistence import Persistence
from nisar.workflows.yaml_argparse import YamlArgparse


# https://stackoverflow.com/a/6993694/112699

def run(cfg: dict):
    '''
    Run COREG workflow with parameters in cfg dictionary
    '''
    info_channel = journal.info("coreg.run")
    info_channel.log("starting COREG")
    t_all = time.time()
    if "reference" in cfg['processing']['target_file_type']: 
#        info_channel.log("unpacking reference, bandpass if neccessary?")
#        bandpass_insar.run(cfg)
        info_channel.log("Converting refrence rdr2geo to enable offsets compute")
        rdr2geo.run(cfg)

    if "secondary" in cfg['processing']['target_file_type']:
        info_channel.log("Unpacking secondary, computing offsets with geo2rdr")
        geo2rdr.run(cfg)
        info_channel.log("Resampling secondaries with offsets")
        resample_slc_v2.run(cfg, 'coarse')

    t_all_elapsed = time.time() - t_all 
    info_channel.log(f"successfully ran in {t_all_elapsed:.3f} seconds")

def load_config(yaml):
    "Load default runconfig, override with user input, and convert to Struct"
    parser = YAML(typ='safe')
    #dir_path = os.path.dirname(os.path.realpath(__file__))
    cfg = parser.load(open(yaml, 'r'))
    #with open(yaml) as f:
    #    user = parser.load(f)
    #helpers.deep_update(cfg, user, flag_none_is_valid=False)
    return cfg


if __name__ == "__main__":
    # parse CLI input
    yaml_parser = YamlArgparse()
    args = yaml_parser.parse()
    # convert CLI input to run configuration
    coreg_runcfg = CoregRunConfig(args)
    run(coreg_runcfg.cfg)
    # To allow persistence, a logfile is required. Raise exception
    # if logfile is None and persistence is requested
    #logfile_path = coreg_runcfg['logging']['path']
   # if (logfile_path is None) and args.restart:
   #     raise ValueError('InSAR workflow persistence requires to specify a logfile')
    #persist = Persistence(logfile_path, args.restart)

    # run InSAR workflow
    #if persist.run:
    #    _, out_paths = h5_prep.get_products_and_paths(insar_runcfg.cfg)
    #    run(insar_runcfg.cfg, out_paths, persist.run_steps)