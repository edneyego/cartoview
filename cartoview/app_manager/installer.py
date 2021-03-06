# -*- coding: utf-8 -*-
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import importlib
import logging
import os
import shutil
import subprocess
import tempfile
import zipfile
import yaml
from builtins import *
from io import BytesIO
from sys import stdout, exit
from threading import Timer
import pkg_resources
import requests
from django.conf import settings
from django.db.models import Max
from future import standard_library

from .config import App as AppConfig
from .models import App, AppStore, AppType
standard_library.install_aliases()
reload(pkg_resources)
formatter = logging.Formatter(
    '[%(asctime)s] p%(process)s  { %(name)s %(pathname)s:%(lineno)d} \
                            %(levelname)s - %(message)s', '%m-%d %H:%M:%S')
logger = logging.getLogger(__name__)
handler = logging.StreamHandler(stdout)
handler.setFormatter(formatter)
logger.addHandler(handler)
current_folder = os.path.abspath(os.path.dirname(__file__))
temp_dir = os.path.join(current_folder, 'temp')


class FinalizeInstaller:
    def __init__(self):
        self.apps_to_finlize = []

    def save_pending_app_to_finlize(self):
        with open(settings.PENDING_APPS, 'wb') as outfile:
            yaml.dump(self.apps_to_finlize, outfile, default_flow_style=False)
        self.apps_to_finlize = []

    def restart_docker(self):
        try:
            exit(1)
        except:
            logger.error(subprocess.Popen("pkill -f python && killall python",
                                          shell=True,
                                          stdout=subprocess.PIPE).stdout.read())

    def restart_server(self, install_app_batch):
        working_dir = os.path.dirname(install_app_batch)
        log_file = os.path.join(working_dir, "install_app_log.txt")
        with open(log_file, 'a') as log:
            proc = subprocess.Popen(
                install_app_batch,
                stdout=log,
                stderr=log,
                shell=True,
                cwd=working_dir)
            logger.warning(proc.stdout)
            logger.error(proc.stderr)

    def finalize_setup(self, app_name):
        self.save_pending_app_to_finlize()
        install_app_batch = getattr(settings, 'INSTALL_APP_BAT', None)
        docker = getattr(settings, 'DOCKER', None)

        def _finalize_setup(app_name):
            if docker:
                try:
                    import cherrypy
                    cherrypy.engine.restart()
                except ImportError:
                    exit(0)
            else:
                self.restart_server(install_app_batch)
        timer = Timer(0.1, _finalize_setup(app_name))
        timer.start()

    def __call__(self, app_name):
        self.finalize_setup(app_name)


FINALIZE_SETUP = FinalizeInstaller()


def serializer_factor(fields):
    class AppSerializer(object):
        __slots__ = fields

        def __init__(self, *args, **kwargs):
            for slot, arg in zip(AppSerializer.__slots__, args):
                setattr(self, slot, arg)
            for key, value in kwargs:
                setattr(self, key, value)

        def get_app_object(self, app):
            obj_property = self.get_property_value
            app.title = self.title
            app.description = self.description
            # TODO:remove short_description
            app.short_description = self.description
            app.owner_url = obj_property('owner_url')
            app.help_url = obj_property('help_url')
            app.author = obj_property('author')
            app.author_website = obj_property('author_website')
            app.home_page = obj_property('demo_url')
            for category in self.type:
                category, created = AppType.objects.get_or_create(
                    name=category)
                app.category.add(category)
            app.status = obj_property('status')
            app.tags.clear()
            app.tags.add(*obj_property('tags'))
            app.license = self.license.get(
                'name', None) if self.license else None
            app.single_instance = obj_property('single_instance')
            return app

        def get_property_value(self, p):
            return getattr(self, p, None)
    return AppSerializer


class AppAlreadyInstalledException(BaseException):
    message = "Application is already installed."


class AppInstaller(object):

    def __init__(self, name, store_id=None, version=None, user=None):
        self.user = user
        self.app_dir = os.path.join(settings.APPS_DIR, name)
        self.name = name
        if store_id is None:
            self.store = AppStore.objects.get(is_default=True)
        else:
            self.store = AppStore.objects.get(id=store_id)
        self.info = self._request_rest_data("app/?name=", name)['objects'][0]
        if version is None or version == 'latest' or self.info[
                "latest_version"]["version"] == version:
            self.version = self.info["latest_version"]
        else:
            data = self._request_rest_data("appversion/?app__name=", name,
                                           "&version=", version)
            self.version = data['objects'][0]
        Serializer = serializer_factor(self.info.keys())
        self.app_serializer = Serializer(
            *[self.info[key] for key in self.info])

    def _request_rest_data(self, *args):
        """
        get app information form app store rest url
        """
        try:
            q = requests.get(self.store.url + ''.join(
                [str(item) for item in args]))
            return q.json()
        except BaseException as e:
            logger.error(e.message)
            return None

    def _download_app(self):
        # TODO: improve download apps (server-side)
        response = requests.get(self.version["download_link"], stream=True)
        zip_ref = zipfile.ZipFile(
            BytesIO(response.content))
        try:
            if not os.path.exists(temp_dir):
                os.makedirs(temp_dir)
            extract_to = tempfile.mkdtemp(dir=temp_dir)
            zip_ref.extractall(extract_to)
            if self.upgrade and os.path.exists(self.app_dir):
                # move old version to temporary dir so that we can restore in
                # case of failure
                old_version_temp_dir = tempfile.mkdtemp(dir=temp_dir)
                shutil.move(self.app_dir, old_version_temp_dir)
            self.old_app_temp_dir = os.path.join(extract_to, self.name)
            shutil.move(self.old_app_temp_dir, settings.APPS_DIR)
            # delete temp extract dir
            shutil.rmtree(extract_to)
        except IOError as e:
            logger.error(e.message)
        finally:
            zip_ref.close()

    def add_app(self, installer):
        # save app configuration
        app, created = App.objects.get_or_create(name=self.name)
        if created:
            if app.order is None or app.order == 0:
                apps = App.objects.all()
                max_value = apps.aggregate(
                    Max('order'))['order__max'] if apps.exists() else 0
                app.order = max_value + 1
            app_config = AppConfig(
                name=self.name, active=True, order=app.order)
            app.apps_config.append(app_config)
            app.apps_config.save()
            app.order = app_config.order
        app = self.app_serializer.get_app_object(app)
        app.version = self.version["version"]
        app.installed_by = self.user
        app.store = AppStore.objects.filter(is_default=True).first()
        app.save()
        return app

    def install(self, restart=True):
        self.upgrade = False
        if os.path.exists(self.app_dir):
            installed_app = App.objects.get(name=self.name)
            if installed_app.version < self.version["version"]:
                self.upgrade = True
            else:
                raise AppAlreadyInstalledException()
        installed_apps = []
        for name, version in list(self.version["dependencies"].items()):
            # use try except because AppInstaller.__init__ will handle upgrade
            # if version not match
            try:
                app_installer = AppInstaller(
                    name, self.store.id, version, user=self.user)
                installed_apps += app_installer.install(restart=False)
            except AppAlreadyInstalledException as e:
                logger.error(e.message)
        self._download_app()
        reload(pkg_resources)
        self.check_then_finlize(restart, installed_apps)
        return installed_apps

    def check_then_finlize(self, restart, installed_apps):
        try:
            installer = importlib.import_module('%s.installer' % self.name)
            installed_apps.append(self.add_app(installer))
            installer.install()
            FINALIZE_SETUP.apps_to_finlize.append(self.name)
            if restart:
                FINALIZE_SETUP(self.name)
        except Exception as ex:
            logger.error(ex.message)

    def uninstall(self, restart=True):
        """
        angular.forEach(app.store.installedApps.objects,
         function(installedApp{
                var currentApp = appsHash[installedApp.name];
                if(dependents.indexOf(currentApp) == -1 &&
                 currentApp.latest_version.dependencies[app.name]){
                    dependents.push(currentApp)
                    _getDependents(currentApp, appsHash, dependents)
                }

            });

        :return:
        """
        installed_apps = App.objects.all()
        for app in installed_apps:
            app_installer = AppInstaller(
                app.name, self.store.id, app.version, user=self.user)
            dependencies = app_installer.version["dependencies"]
            if self.name in dependencies:
                app_installer.uninstall(restart=False)
        installer = importlib.import_module('%s.installer' % self.name)
        installer.uninstall()
        from django.contrib.contenttypes.models import ContentType
        ContentType.objects.filter(app_label=self.name).delete()
        app = App.objects.get(name=self.name)
        app.delete()

        app_config = app.apps_config.get_by_name(self.name)
        del app.apps_config[app_config]
        app.apps_config.save()

        app_dir = os.path.join(settings.APPS_DIR, self.name)
        shutil.rmtree(app_dir)
        if restart:
            FINALIZE_SETUP(app.name)
