# pylint: disable=invalid-name,no-member,too-many-arguments

from argparse import Namespace
import os
import shlex
import shutil
import subprocess

import jinja2 as j2
import yaml

import f90nml

import errors
import utils

class BatchJob():

    def __init__(self, config, machine, starttime, **kwargs):

        self.config = self.config_namespace(config)
        self.machine = self.config_namespace(machine)
        self.starttime = starttime

        overwrite = kwargs.get('overwrite', False)
        self.workdir = self.create_workdir(overwrite)

    @staticmethod
    def config_namespace(config):

        ns = Namespace()
        utils.namespace(ns, config)
        return ns

    def create_workdir(self, overwrite=True):

        cycle = self.starttime.strftime('%Y%m%d%H')

        workdir = self.config.paths.workdir.format(
            n=self.config,
            cycle=cycle
            )

        if os.path.exists(workdir):
            if overwrite:
                shutil.rmtree(workdir)
            else:
                msg = f"create_workdir: {workdir} exists & will not be removed!"
                raise errors.DirectoryExists(msg)

        os.makedirs(workdir)

        return workdir

    def stage_files(self, action, links):

        if not links:
            return

        if action not in ['copy', 'link']:
            msg = 'link_static_files: action = {action} is not copy or link.'
            raise ValueError(msg)

        if not isinstance(links, dict):
            msg = f'link_static_files: links is type {type(links)}, expected dict.'
            raise ValueError(msg)

        n = self.config
        starttime = self.starttime.strftime('%Y%m%d%H')

        files = []
        for path_name, filelist in links.items():

            path_dir = vars(n.paths).get(
                path_name,
                vars(self.machine.dirs).get(path_name),
                )

            if not path_dir:
                msg = f'stage_files: cannot find a path entry for {path_name}'
                raise ValueError(msg)

            for src_dst in filelist:

                # First item of list will be the name of the source. Join with
                # the path specified by the section header.
                filepath = os.path.join(path_dir, src_dst[0])

                # Last item of list will be the name of the destination
                dest_name = src_dst[-1]
                destination = os.path.join(self.workdir, dest_name)

                # Add the processed src_dst to the filelist
                files.append((filepath, destination))

        for src, dst in files:
            if action == 'link':
                print(f'Linking {src.format(n=n, starttime=starttime)} to {dst}')
                utils.safe_link(src.format(n=n, starttime=starttime), dst)
            if action == 'copy':
                print(f'Copying {src.format(n=n, starttime=starttime)} to {dst}')
                utils.safe_copy(src.format(n=n, starttime=starttime), dst)

    @staticmethod
    def create_yml(outfile, settings):

        with open(outfile, 'w') as fn:
            yaml.dump(settings, fn)


    @staticmethod
    def render_template(outfile, template, tmpl_vars):

        with open(template, 'r') as tmpl_file:
            template = j2.Template(tmpl_file.read())

        xml_contents = template.render(**tmpl_vars)

        with open(outfile, 'w') as out_file:
            out_file.write(xml_contents)

    @property
    def executable(self):
        pass

    def parallel_run(self, exe):

        run_cmd = self.machine.run_command.format(n=self.config)
        cmd = shlex.split(f'{run_cmd} {exe}')
        pc = subprocess.run(cmd, check=True, stdout=subprocess.PIPE)

        while True:
            output = pc.stdout.readline()
            if not output and pc.poll():
                break
            if output:
                print(output.strip())

        rc = pc.poll()
        return rc


class Forecast(BatchJob):

    def __init__(self, config, machine, starttime, **kwargs):

        BatchJob.__init__(self, config, machine, starttime, **kwargs)

        # Set of configuration Namespace objects.
        grid = self.config_namespace(kwargs.get('grid'))
        self.grid = grid

        self.nml = {}
        for sect, keys in kwargs.get('nml', {}).items():
            self.nml[sect] = {}
            for key, value in keys.items():
                if isinstance(value, str):
                    try:
                        tmp = value.format(grid=self.grid, n=self.config)

                    except AttributeError:
                        tmp = eval("f'{}'".format(value))

                    self.nml[sect][key] = utils.to_number(tmp)

                elif isinstance(value, list):
                    tmp = [v.format(grid=self.grid, n=self.config) for v in value]
                    self.nml[sect][key] = [utils.to_number(v) for v in tmp]
                else:
                    self.nml[sect][key] = value

    def run(self, dry_run=False):

        # Create INPUT dir
        os.makedirs(os.path.join(self.workdir, 'INPUT'))
        os.makedirs(os.path.join(self.workdir, 'RESTART'))

        # Link/copy in static and cycle dependent files
        for section in ['static', 'cycledep']:
            self.stage_all(section)

        # Create diag_table
        self.create_diag_table()

        # Create model_config
        self.create_model_config()

        # Create input.nml
        self.create_nml()

        # Run the forecast
        if not dry_run:
            self.parallel_run(self.config.static.copy.fv3_exec[0])

    def create_diag_table(self):

        outfile = os.path.join(self.workdir, 'diag_table')
        template = self.config.paths.diag_tmpl.format(n=self.config)
        template_vars = {
            'res': self.grid.res,
            'starttime': self.starttime,
            }
        self.render_template(outfile, template, template_vars)

    def create_model_config(self):

        # Output file
        model_config_out = os.path.join(self.workdir, 'model_configure')

        # Aliasing object variables for consistency with YAML
        # pylint: disable=possibly-unused-variable
        config = self.config
        grid = self.grid
        machine = self.machine
        # pylint: enable=possibly-unused-variable

        # Start with config items set in fv3_script list
        model_config_items = config.model_config

        # Loop through each item and add it to the model_config dict
        model_config = {}

        for item in model_config_items:

            # Each item should be a key, value pair, or a single string item.
            if isinstance(item, dict):

                # For key, value pairs, set the model_config dict with the key,
                # and use the value as a reference to a configuration set by one
                # of the object variables: config, grid, machine, etc. If this
                # the value does not reference a local variable, then set it to
                # the value provided in the config.
                for key, value in item.items():
                    var_val = locals().get(value)
                    value = var_val.__dict__.get(key, value) if var_val else value
                    value = f'.{str(value).lower()}.' if isinstance(value, bool) else value

                    model_config[key] = value

            else: # item is a single item string
                try:
                    # Call the method named by the item
                    model_config.update(self.__getattribute__(item)())

                except KeyError:
                    msg = f'{__name__}: {item} is not an available method!'
                    print(msg)

        self.create_yml(model_config_out, model_config)

    def _pe_member01(self):

        mpi_tasks = self.grid.layout_x * self.grid.layout_y

        if self.config.quilting:
            mpi_tasks += self.grid.quilting.write_groups * self.grid.quilting.write_tasks_per_group

        # The number of processors will be used when running non-slurm
        # subprocess. Make note of it in the config.
        self.config.__dict__['nproc'] = mpi_tasks

        return {'PE_MEMBER01': mpi_tasks}

    def _quilting(self):

        quilting = self.config.quilting
        quilting = f'.{str(quilting).lower()}.' if isinstance(quilting, bool) else quilting
        ret = {'quilting': quilting}

        # Add the grid section to the returned dict.
        if self.config.quilting:
            ret.update(vars(self.grid.quilting))

        return ret

    def _start_times(self):
        times = ['year', 'month', 'day', 'hour', 'minute', 'second']
        return {f'start_{t}': int(self.starttime.__getattribute__(t)) for t in times}


    def create_nml(self):

        fv3_nml = os.path.join(self.workdir, 'input.nml')

        # Read in the namelist that has all the base settings.
        with open(self.config.paths.base_nml.format(n=self.config), 'r') as nml_file:
            base_nml = f90nml.read(nml_file)


        # Update the base namelist with settings for the current configuration
        # Send self.nml, a Namespace object, as dict to update_dict.
        # Update_dict modifies dict in place.
        utils.update_dict(base_nml, self.nml)

        with open(fv3_nml, 'w') as fn:
            base_nml.write(fn)

    def namsfc_files(self):

        '''
        A "callable" used by stage_all. Must return a list of lists. 
        Returns the list of namsfc files from the namelist section.
        '''

        filedict = vars(self.config.namelist.namsfc)
        return [[fn] for key, fn in filedict.items() if key[:2] == 'fn' and len(fn.split('.')) > 1]


    def stage_all(self, section):

        allowed_sections = ['static', 'cycledep']
        if section not in allowed_sections:
            msg = f'stage_all: {section} is not in {allowed_sections}.'
            raise ValueError(msg)

        file_section = vars(self.config).get(section)

        n = self.config
        g = self.grid

        all_files = {
                'copy': {},
                'link': {},
                }

        for action in all_files.keys():
            files_to_stage = vars(file_section).get(action)
            if files_to_stage:
                for path_name, files in vars(files_to_stage).items():


                    all_files[action][path_name] = []

                    for list_item in files:

                        # files_to_stage items can either be a list or a callable
                        if isinstance(list_item, list):
                            all_files[action][path_name].append([tmpl.format(n=n, g=g) for tmpl in list_item])

                        elif callable(self.__getattribute__(list_item)):

                            # Note: any callable function supported by this functionality must return a list of lists, as
                            # expected by the all_files dict.
                            all_files[action][path_name].extend(self.__getattribute__(list_item)())

                        else:

                            msg = f'stage_all: {list_item} in {path_name} is not a list or callable!'
                            raise errors.InvalidConfigSetting(msg)



        for action in ['copy', 'link']:
            self.stage_files(action, all_files[action])
