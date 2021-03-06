import requests
from cartoview.app_manager.installer import serializer_factor
from cartoview.app_manager.models import App, AppStore
from django.core.management.base import BaseCommand

store = AppStore.objects.get(is_default=True)


class Command(BaseCommand):
    help = 'Update existing apps'

    def handle(self, *args, **options):
        for index, app in enumerate(App.objects.all()):
            try:
                if app.store:
                    data = self.get_data_from_store(app.name, app.store.url)
                    Serializer = serializer_factor(data.keys())
                    app_serializer = Serializer(*[data[key] for key in data])
                    app = app_serializer.get_app_object(app)
                    app.save()
                    print('[{}] {}  updated'.format(index + 1, app.name))
                else:
                    print('[{}] {}  Ignored because No Store Available'.format(
                        index + 1, app.name))
            except Exception as ex:
                print('[{}] {}  Failed error message {}'.format(
                    index + 1, app.name, ex.message))

    def get_data_from_store(self, appname, url):
        payload = {'name__exact': appname}
        req = requests.get(url + "app", params=payload)
        json_api = req.json()
        app_data = json_api.get('objects')[0]
        return app_data
