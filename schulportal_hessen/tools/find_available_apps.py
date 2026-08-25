from schulportal_hessen.base import SchulportalHessenAPI


def find_available_apps(debug: bool = False):
    api = SchulportalHessenAPI()
    Overview = api.get_apps()
    available_apps = []

    for app in Overview['data']['entrys']:
        # print(app)
        available_apps.append((app['Name'], app))
        if debug:
            print("Found", app['Name'], "app!", app['link'])

    return available_apps
