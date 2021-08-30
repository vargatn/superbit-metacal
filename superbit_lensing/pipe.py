from abc import ABC, abstractmethod
from glob import glob
import os
import yaml
import utils
import logging
import subprocess
import superbit_lensing as sb

# TODO: We should move this stuff into a separate diagnostics.py file
#       at some point
import matplotlib.pyplot as plt
from astropy.table import Table

import pudb

class SuperBITModule(dict):
    '''
    Class for an arbitrary module in the SuperBIT weak lensing pipeline

    These modules are not intended to be run on their own, but as components
    called in the `SuperBITPipeline` class

    The pipeline handles the usual things like logging, verbosity, etc.
    '''

    _req_fields = []
    _opt_fields = []

    def __init__(self, name, config, set_defaults=False):

        self.name = name

        if not isinstance(config, dict):
            raise TypeError(f'The passed config for module {name} was not a dict!')

        self._config = config

        self._check_config(set_defaults=set_defaults)

        self.plotdir = None

        return

    def _check_config(self, set_defaults=False):
        '''
        Make sure all required elements of the module config are present.
        '''

        # Check that required fields are there
        for field in self._req_fields:
            if field not in self._config.keys():
                raise KeyError(f'Field "{field}" must be present in the {self.name} config!')

        # Make sure there are no surprises
        for field in self._config.keys():
            if (field not in self._req_fields) and (field not in self._opt_fields):
                raise KeyError(f'{field} is not a valid field for the {self.name} module!')

        # Make sure all fields are at least initialized to None
        if set_defaults is True:
            for field in self._opt_fields:
                if field not in self._config.keys():
                    self._config[field] = None

        # anything else?
        # ...

        return

    def _run_setup(self, logprint):
        logprint(f'Starting module {self.name}')

        # ...

        return

    def _setup_options(self, run_options):
        options = ''
        # for opt in self._opt_fields:
            # if opt in self._config:
            #     options += f' --{opt}={self._config[opt]}'

        vb = ' -v' if run_options['vb'] is True else ''
        options += vb

        return options

    def _run_cleanup(self, logprint):

        logprint(f'Module {self.name} completed succesfully')

        # ...
        return

    @abstractmethod
    def run(self, run_options, logprint):
        pass

    def run_diagnostics(self, run_options, logprint):
        pass

    def _run_command(self, cmd, logprint):
        '''
        Run bash command
        '''

        logprint(cmd)

        # for live prints but no error handling:
        # process = subprocess.Popen(cmd.split(),
        #                            stdout=subprocess.PIPE,
        #                            stderr=subprocess.PIPE,
        #                            bufsize=1)

        # for line in iter(process.stdout.readline, b''):
        #     logprint(line.decode('utf-8').replace('\n', ''))

        # output, error = process.communicate()

        args = [cmd.split()]
        kwargs = {'stdout':subprocess.PIPE,
                  # TODO: check this!
                  # 'stderr':subprocess.PIPE,
                  'stderr':subprocess.STDOUT,
                  'bufsize':1}
        with subprocess.Popen(*args, **kwargs) as process:
            try:
                for line in iter(process.stdout.readline, b''):
                    logprint(utils.decode(line).replace('\n', ''))

                stdout, stderr = process.communicate()

            except:
                process.kill()
                raise
                # stdout, stderr = process.communicate()
                # logprint('\n'+utils.decode(stderr))

                # return 1
                # process.kill()

            rc = process.poll()

            if rc:
                # stdout, stderr = process.communicate()
                err = subprocess.CalledProcessError(rc,
                                                    process.args,
                                                    output=stdout,
                                                    stderr=stderr)

                logprint('\n'+utils.decode(err.stderr))
                raise err

                # return rc

        # try:
        #     process = subprocess.run(cmd.split(),
        #                              stdout=subprocess.PIPE,
        #                              stderr=subprocess.PIPE,
        #                              # stderr=subprocess.PIPE,
        #                              # stderr=subprocess.STDOUT,
        #                              bufsize=1,
        #                              check=True)
        # except subprocess.CalledProcessError as e:
        #     logprint(e.stderr)
        #     return 1

        # output, error = process.communicate()
        # rc = process.returncode

        # if rc != 0:
        #     logprint(f'call returned in error with rc={rc}:')
        #     logprint(output)
        #     logprint(error)
        #     return rc

        # process.wait()

        # now get the return code from the executed process
        # streamdata = process.communicate()[0]
        # res = process.communicate()
        rc = process.returncode

        # process.stdout.close()

        return rc

    def __getitem__(self, key):
        val = self._config.__getitem__(self, key)

        return val

    def __setitem__(self, key, val):

        self._config.__setitem__(self, key, val)

        return

class SuperBITPipeline(SuperBITModule):

    # We abuse the required fields variable a bit here, as
    # the pipeline module is atypical.
    _req_fields = ['run_options']
    _req_run_options_fields = ['run_name', 'order', 'vb']

    # This is how modules are registered
    # _opt_fields = {get_module_types().keys()}
    _opt_fields = [] # Gets setup in constructor
    _opt_run_options_fields = ['ncores', 'diagnostics']

    def __init__(self, config_file, log=None):

        config = utils.read_yaml(config_file)

        self._setup_opt_fields()

        super(SuperBITPipeline, self).__init__('pipeline',
                                               config)
                                               # set_defaults=False)
        self._check_config()

        if self._config['run_options']['ncores'] is None:
            # use half by default
            ncores = os.cpu_count() // 2
            self._config['run_options']['ncores'] = ncores

        col = 'diagnostics'
        # if hasattr(self._config['run_options'], col):
        if col in self._config['run_options']:
            self.diagnostics = self._config['run_options'][col]
        else:
            self.diagnostics = False

        self.log = log
        self.vb = self._config['run_options']['vb']

        self.logprint = utils.LogPrint(log, self.vb)

        module_names = list(self._config['run_options']['order'])
        # module_names.remove('run_options') # Not really a module

        self.modules = []
        for name in module_names:
            self.modules.append(build_module(name,
                                             self._config[name],
                                             self.logprint))

        return

    def _check_config(self, set_defaults=False):
        '''
        Make sure all required elements of the module config are present.
        '''

        # super(SuperBITPipeline, self)._check_config(set_defaults=set_defaults)

        # The pipeline module required fields variable is a bit special
        for field in self._req_run_options_fields:
            if field not in self._config['run_options'].keys():
                raise KeyError(f'Must have an entry for "{field}" in the "run_options" ' + \
                'field for the {self.name} config!')

        if set_defaults is True:
            for field in self._opt_run_options_fields:
                if field not in self._config['run_options'].keys():
                    self._config['run_options'][field] = None

        return

    def _setup_opt_fields(cls):
        cls._opt_fields = MODULE_TYPES.keys()

        return

    def run(self):
        '''
        Run each module in order
        '''

        self.logprint('Starting pipeline run')
        for module in self.modules:
            rc = module.run(self._config['run_options'], self.logprint)

            if rc !=0:
                self.logprint.warning(f'Exception occured during {module.name}.run()')
                return 1

            if self.diagnostics is True:
                module.run_diagnostics(self._config['run_options'], self.logprint)

        return 0

class GalSimModule(SuperBITModule):
    _req_fields = ['config_file', 'outdir']
    _opt_fields = ['config_dir', 'vb', 'use_mpi', 'run_name']

    def __init__(self, name, config):
        super(GalSimModule, self).__init__(name, config)

        self.gs_config_path = None
        self.gs_config = None

        return

    def run(self, run_options, logprint):
        '''
        Relevant type checks and param init's have already
        taken place
        '''

        logprint(f'Running module {self.name}')
        logprint(f'config:\n{self._config}')

        self.gs_config_path = os.path.join(self._config['config_dir'],
                                           self._config['config_file'])
        self.gs_config = utils.read_yaml(self.gs_config_path) # Do we need this?

        cmd = self._setup_run_command(run_options)

        rc = self._run_command(cmd, logprint)
        # rc = utils.run_command(cmd, logprint)

        return rc

    def _setup_run_command(self, run_options):

        galsim_dir = os.path.join(utils.MODULE_DIR, 'galsim')
        galsim_filepath = os.path.join(galsim_dir, 'mock_superBIT_data.py')

        outdir = self._config['outdir']
        base = f'python {galsim_filepath} {self.gs_config_path} --outdir={outdir}'

        options = self._setup_options(run_options)

        # if not hasattr(self._config, 'run_name'):
        if 'run_name' not in self._config:
            run_name = run_options['run_name']
            options += f' --run_name={run_name}'

        # options = f'--run_name={run_name} --outdir={outdir}' + vb

        cmd = base + options

        ncores = run_options['ncores']
        if ncores > 1:
            cmd = f'mpiexec -n {ncores} ' + cmd

        return cmd

    def run_diagnostics(self, run_options, logprint):

        super(GalSimModule, self).run_diagnostics(run_options, logprint)

        logprint(f'Running diagnostics for {self.name}')

        plotdir = os.path.join(self._config['outdir'], 'plots')
        plot_outdir = os.path.join(plotdir, self.name)

        for d in [plotdir, plot_outdir]:
            if not os.path.exists(d):
                os.mkdir(d)

        self.plotdir = plot_outdir

        ## Check consistency of truth tables
        self.plot_compare_truths(run_options, logprint)

        return

    def plot_compare_truths(self, run_options, logprint):
        # TODO: Not obvious to me why there are multiple tables - this here
        # just to prove this. Can remove later

        truth_tables = glob(os.path.join(self._config['outdir'], 'truth*.fits'))
        N = len(truth_tables)

        tables = []
        for fname in truth_tables:
            tables.append(Table.read(fname))

        cols = ['ra', 'flux', 'hlr']
        Nrows = len(cols)
        Nbins = 30
        ec = 'k'
        alpha = 0.75

        for i in range(1, Nrows+1):
            plt.subplot(Nrows, 1, i)

            col = cols[i-1]

            k = 1
            for t in tables:
                plt.hist(t[col], bins=Nbins, ec=ec, alpha=alpha, label=f'Truth_{k}')
                k += 1

            plt.xlabel(f'True {col}')
            plt.ylabel('Counts')
            plt.legend()

            if ('flux' in col) or ('hlr' in col):
                plt.yscale('log')

        plt.gcf().set_size_inches(9, 3*Nrows+2)

        outfile = os.path.join(self.plotdir, 'compare_truth_tables.pdf')
        plt.savefig(outfile, bbox_inches='tight')

        return

class MedsmakerModule(SuperBITModule):
    _req_fields = ['mock_dir', 'outfile']
    _opt_fields = ['fname_base', 'meds_coadd', 'clobber', 'source_select',
                   'cut_stars', 'vb']

    def __init__(self, name, config):
        super(MedsmakerModule, self).__init__(name, config)

        # ...

        return

    def run(self, run_options, logprint):
        logprint(f'Running module {self.name}')
        logprint(f'config:\n{self._config}')

        cmd = self._setup_run_command(run_options)

        rc = self._run_command(cmd, logprint)

        return rc

    def _setup_run_command(self, run_options):


        mock_dir = self._config['mock_dir']
        outfile = self._config['outfile']

        filepath = os.path.join(utils.MODULE_DIR,
                                'medsmaker',
                                'scripts',
                                'process_mocks.py')

        base = f'python {filepath} {mock_dir} {outfile}'

        options = self._setup_options(run_options)

        cmd = base + options

        return cmd

class MetacalModule(SuperBITModule):
    _req_fields = []
    _opt_fields = ['config_file', 'config_dir']

    def run(self, run_options, logprint):
        logprint(f'Warning: run method for {self.name} module not yet implemented!')
        logprint('Normally this would error, but doing test runs.')

        # cmd = self._setup_run_comand(run_options)

        # rc = self._run_command(cmd, logprint)
        rc = 0

        return rc

class ShearProfileModule(SuperBITModule):
    _req_fields = []
    _opt_fields = []

    # will need to implement, but this way it will error until then
    # def run(self, run_options, logprint):
        # cmd = self._setup_run_comand(run_options)

        # self._run_command(cmd, logprint)

        # return

def build_module(name, config, logprint):
    name = name.lower()

    if name in MODULE_TYPES.keys():
        # User-defined input construction
        module = MODULE_TYPES[name](name, config)
    else:
        # Attempt generic input construction
        logprint(f'Warning: {name} is not a pre-defined module type.')
        logprint('Attempting generic module construction.')
        logprint('Module is not guaranteed to run succesfully.')

        module = SuperBITModule(name, config)

    return module

def make_test_config(config_file='pipe_test.yaml', outdir=None, clobber=False):
    if outdir is not None:
        filename = os.path.join(outdir, config_file)

    if (clobber is True) or (not os.path.exists(filename)):
        run_name = 'pipe_test'
        with open(filename, 'w') as f:
            # Create dummy config file
            CONFIG = {
                'run_options': {
                    'run_name': run_name,
                    # 'outdir':
                    'vb': True,
                    'ncores': 4,
                    'diagnostics': True,
                    'order': [
                        'galsim',
                        'medsmaker',
                        'metacal'
                        ]
                },
                'galsim': {
                    # 'config_file': 'pipe_test.yaml',
                    'config_file': 'superbit_parameters_forecast.yaml',
                    'config_dir': os.path.join(utils.MODULE_DIR,
                                               'galsim',
                                               'config_files'),
                    'outdir': os.path.join(utils.TEST_DIR,
                                           run_name)
                },
                'medsmaker': {
                    'mock_dir': os.path.join(utils.TEST_DIR,
                                             run_name),
                    'outfile': f'{run_name}_meds.fits'
                },
                'metacal': {
                    'config_file': 'test.yaml',
                    'config_dir': 'test'
                },
            }

            yaml.dump(CONFIG, f, default_flow_style=False)

    return filename

def get_module_types():
    return MODULE_TYPES

# NOTE: This is where you must register a new module
MODULE_TYPES = {
    'galsim' : GalSimModule,
    'medsmaker' : MedsmakerModule,
    'metacal' : MetacalModule,
    'shear-profile' : ShearProfileModule,
    }

def main():

    testdir = utils.get_test_dir()

    logfile = 'pipe_test.log'
    logdir = os.path.join(testdir, 'pipe_test')
    log = utils.setup_logger(logfile, logdir=logdir)

    config_file = make_test_config(clobber=True, outdir=logdir)

    config = utils.read_yaml(config_file)
    vb = config['run_options']['vb']

    if vb:
        print(f'config =\n{config}')

    pipe = SuperBITPipeline(config_file, log=log)

    rc = pipe.run()

    return rc

if __name__ == '__main__':
    rc = main()

    if rc == 0:
        print('Tests have completed without errors')
    else:
        print(f'Tests failed with rc={rc}')