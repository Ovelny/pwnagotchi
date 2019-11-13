import logging
import os
import base64
import _thread
import secrets
from functools import wraps

# https://stackoverflow.com/questions/14888799/disable-console-messages-in-flask-server
logging.getLogger('werkzeug').setLevel(logging.ERROR)
os.environ['WERKZEUG_RUN_MAIN'] = 'true'

import pwnagotchi
import pwnagotchi.grid as grid
import pwnagotchi.ui.web as web
from pwnagotchi import plugins

from flask import send_file
from flask import Response
from flask import request
from flask import jsonify
from flask import abort
from flask import redirect
from flask import render_template


class Handler:
    def __init__(self, config, agent, app):
        self._config = config
        self._agent = agent
        self._app = app

        self._app.add_url_rule('/', 'index', self.with_auth(self.index))
        self._app.add_url_rule('/state/', 'state', self.state, defaults={'format': 'json'}, methods=['GET'])
        self._app.add_url_rule('/state/<format>', 'state', self.state, methods=['GET'])

        self._app.add_url_rule('/shutdown', 'shutdown', self.with_auth(self.shutdown), methods=['POST'])
        self._app.add_url_rule('/restart', 'restart', self.with_auth(self.restart), methods=['POST'])

        # inbox
        self._app.add_url_rule('/inbox', 'inbox', self.with_auth(self.inbox))
        self._app.add_url_rule('/inbox/profile', 'inbox_profile', self.with_auth(self.inbox_profile))
        self._app.add_url_rule('/inbox/peers', 'inbox_peers', self.with_auth(self.inbox_peers))
        self._app.add_url_rule('/inbox/<id>', 'show_message', self.with_auth(self.show_message))
        self._app.add_url_rule('/inbox/<id>/<mark>', 'mark_message', self.with_auth(self.mark_message))
        self._app.add_url_rule('/inbox/new', 'new_message', self.with_auth(self.new_message))
        self._app.add_url_rule('/inbox/send', 'send_message', self.with_auth(self.send_message), methods=['POST'])

        # plugins
        plugins_with_auth = self.with_auth(self.plugins)
        self._app.add_url_rule('/plugins', 'plugins', plugins_with_auth, strict_slashes=False,
                               defaults={'name': None, 'subpath': None})
        self._app.add_url_rule('/plugins/<name>', 'plugins', plugins_with_auth, strict_slashes=False,
                               methods=['GET', 'POST'], defaults={'subpath': None})
        self._app.add_url_rule('/plugins/<name>/<path:subpath>', 'plugins', plugins_with_auth, methods=['GET', 'POST'])

    def _check_creds(self, u, p):
        # trying to be timing attack safe
        return secrets.compare_digest(u, self._config['username']) and \
               secrets.compare_digest(p, self._config['password'])

    def with_auth(self, f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            auth = request.authorization
            if not auth or not auth.username or not auth.password or not self._check_creds(auth.username, auth.password):
                return Response('Unauthorized', 401, {'WWW-Authenticate': 'Basic realm="Unauthorized"'})
            return f(*args, **kwargs)

        return wrapper

    def _return_json(self):

        display = self._agent.view()

        mesh_data = grid.call("/mesh/data")
        mesh_peers = grid.peers()
        messages = grid.inbox()

        total_messages = len(messages)
        unread_messages = len([m for m in messages if m['seen_at'] is None])

        peers = []
        for peer in mesh_peers:
            peers.append({
                "identity": peer["advertisement"]["identity"],
                "name": peer["advertisement"]["name"],
                "face": peer["advertisement"]["face"],
                "pwnd_run": peer["advertisement"]["pwnd_run"],
                "pwnd_tot": peer["advertisement"]["pwnd_tot"],
            })

        result = {
            "identity": mesh_data["identity"],
            "epoch": mesh_data["epoch"],
            "status": display.get('status'),
            "channel_text": display.get('channel'),
            "aps_text": display.get('aps'),
            "apt_tot": self._agent.get_total_aps(),
            "aps_on_channel": self._agent.get_aps_on_channel(),
            "channel": self._agent.get_current_channel(),
            "uptime": display.get('uptime'),
            "mode": display.get('mode'),
            "name": mesh_data["name"],
            "face": mesh_data["face"],
            "num_peers": len(mesh_peers),
            "peers": peers,
            "total_messages": total_messages,
            "unread_messages": unread_messages,
            "friend_face_text": display.get('friend_face'),
            "friend_name_text": display.get('friend_name'),
            "pwnd_run": mesh_data["pwnd_run"],
            "pwnd_tot": mesh_data["pwnd_tot"],
            "version": pwnagotchi.version,
            "memory": pwnagotchi.mem_usage(),  # Scale 0-1
            "cpu": pwnagotchi.cpu_load(),  # Scale 0-1
            "temperature": pwnagotchi.temperature()  # Degrees C
        }

        return jsonify(result)

    def _return_png(self):
        with web.frame_lock:
            return send_file(web.frame_path, mimetype="image/png")

    def index(self):
        theme = "theme-default.html"
        theme_config_location = self._agent.config()["ui"]["display"]["video"]

        if "theme" in theme_config_location:
            theme = "theme-" + theme_config_location["theme"] + ".html"

        return render_template(theme,
                               title=pwnagotchi.name(),
                               other_mode='AUTO' if self._agent.mode == 'manual' else 'MANU',
                               fingerprint=self._agent.fingerprint())

    def inbox(self):
        page = request.args.get("p", default=1, type=int)
        inbox = {
            "pages": 1,
            "records": 0,
            "messages": []
        }
        error = None

        try:
            inbox = grid.inbox(page, with_pager=True)
        except Exception as e:
            logging.exception('error while reading pwnmail inbox')
            error = str(e)

        return render_template('inbox.html',
                               name=pwnagotchi.name(),
                               page=page,
                               error=error,
                               inbox=inbox)

    def inbox_profile(self):
        data = {}
        error = None

        try:
            data = grid.get_advertisement_data()
        except Exception as e:
            logging.exception('error while reading pwngrid data')
            error = str(e)

        return render_template('profile.html',
                               name=pwnagotchi.name(),
                               fingerprint=self._agent.fingerprint(),
                               data=data,
                               error=error)

    def inbox_peers(self):
        peers = {}
        error = None

        try:
            peers = grid.memory()
        except Exception as e:
            logging.exception('error while reading pwngrid peers')
            error = str(e)

        return render_template('peers.html',
                               name=pwnagotchi.name(),
                               peers=peers,
                               error=error)

    def show_message(self, id):
        message = {}
        error = None

        try:
            message = grid.inbox_message(id)
            if message['data']:
                message['data'] = base64.b64decode(message['data']).decode("utf-8")
        except Exception as e:
            logging.exception('error while reading pwnmail message %d' % int(id))
            error = str(e)

        return render_template('message.html',
                               name=pwnagotchi.name(),
                               error=error,
                               message=message)

    def new_message(self):
        to = request.args.get("to", default="")
        return render_template('new_message.html', to=to)

    def send_message(self):
        to = request.form["to"]
        message = request.form["message"]
        error = None

        try:
            grid.send_message(to, message)
        except Exception as e:
            error = str(e)

        return jsonify({"error": error})

    def mark_message(self, id, mark):
        logging.info("marking message %d as %s" % (int(id), mark))
        grid.mark_message(id, mark)
        return redirect("/inbox")

    def state(self, format):
        if format not in ["json", "png"]:
            return abort(415)

        if format == "png":
            return self._return_png()

        return self._return_json()

    def plugins(self, name, subpath):
        if name is None:
            # show plugins overview
            abort(404)
        else:
            if name in plugins.loaded and hasattr(plugins.loaded[name], 'on_webhook'):
                try:
                    return plugins.loaded[name].on_webhook(subpath, request)
                except Exception:
                    abort(500)
            else:
                abort(404)

    # serve a message and shuts down the unit
    def shutdown(self):
        try:
            return render_template('status.html', title=pwnagotchi.name(), go_back_after=60,
                                   message='Shutting down ...')
        finally:
            _thread.start_new_thread(pwnagotchi.shutdown, ())

    # serve a message and restart the unit in the other mode
    def restart(self):
        mode = request.form['mode']
        if mode not in ('AUTO', 'MANU'):
            mode = 'MANU'

        try:
            return render_template('status.html', title=pwnagotchi.name(), go_back_after=30,
                                   message='Restarting in %s mode ...' % mode)
        finally:
            _thread.start_new_thread(pwnagotchi.restart, (mode,))

