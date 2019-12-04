from flask import Flask, send_from_directory, redirect, request, Response
from flask_socketio import SocketIO, emit
from os.path import join, dirname, abspath
import threading
import time
import json
import sys
import queue
import filters
import sources
from static import settings
from static import praw_wrapper
from static import metadata
from interfaces import UserInterface
from processing.wrappers import SanitizedRelFile
from processing.controller import RMDController
import sql
import eventlet
eventlet.monkey_patch()

webdir = abspath(join(dirname(__file__), '../web/'))


class WebUI(UserInterface):
	def __init__(self):
		super().__init__(ui_id="web")
		self._session = sql.session()
		self.controller = None
		self._stat_cache = None
		self.app, self.socket = _create(self)

	def display(self):
		threading.Thread(target=self._queue_reader, daemon=True).start()
		self.socket.run(self.app, port=8080)

	@property
	def running(self):
		return True  # TODO: Fix this.

	def _queue_reader(self):
		print('Deployed queue event relay.')
		while True:
			try:
				while self.controller:
					event = self.controller.event_queue.get_nowait()
					self.socket.emit('status_update', event)
			except queue.Empty:
				pass
			time.sleep(0.25)

	def get_cached_stats(self):
		""" These stats can be slow to look up, so they're aggressively cached and reloaded only after downloading. """
		if not self._stat_cache:
			self._stat_cache = {
				'total_files': self._session.query(sql.File).count(),
				'total_files_dl': self._session.query(sql.File).filter(sql.File.downloaded).count(),
				'total_submissions': self._session.query(sql.Post).filter(sql.Post.type == 'Submission').count(),
				'total_comments': self._session.query(sql.Post).filter(sql.Post.type == 'Comment').count(),
				'total_urls': self._session.query(sql.URL).filter(sql.URL.processed).count(),
				'total_urls_failed': self._session.query(sql.URL).filter(sql.URL.failed).count()
			}
		return self._stat_cache


def _create(self):
	""" Build the server app and all its routes """
	app = Flask(__name__)
	app.config['SECRET_KEY'] = '[rmd secret session key]'
	socketio = SocketIO(app)

	@app.route('/')
	def index():
		return redirect("/index.html", code=302)

	@app.route('/<path:path>')
	def send_static(path):
		return send_from_directory(webdir, path)

	@socketio.on('my event')
	def test_message(message):
		print('ws:', message)
		emit('my response', {'data': 'got it!'})
		return 'woo'

	@app.route('/file')
	def downloaded_files():
		""" Allows the UI to request files RMD has scraped.
			In format: "./file?id=file_token"
		"""
		token = request.args.get('id')
		file_obj = self._session.query(sql.File).filter(sql.File.id == token).first()
		file_path = file_obj.path
		print('Requested RMD File: %s, %s' % (settings.get("output.base_dir"), file_path))
		try:
			return send_from_directory(settings.get("output.base_dir"), file_path)
		except Exception as ex:
			print(ex)

	@app.route('/authorize')
	def authorize_rmd_token():
		state = str(request.args.get('state'))
		print('New refresh code request: ', state, request.args.get('code'))
		if state.strip() == settings.get('auth.oauth_key').strip():
			code = request.args.get('code')
			print('Saving new reddit code.')
			refresh = praw_wrapper.get_refresh_token(code)
			if refresh:
				settings.put('auth.refresh_token', refresh)
				return 'Saved authorization token! Close this page to continue.'
		return 'Cannot save the new auth key, something went wrong.<br><a href="../index.html">Back</a>'

	@app.route('/api/version')
	def get_version():
		return {'current_version': metadata.current_version}

	@app.route('/api/oauth_url')
	def api_get_oauth_url():
		port = 7505
		url = False
		message = ''
		if settings.get('interface.port') != port:
			message = 'The UI is not using the default port (%s), and cannot use the Web App to authenticate! ' \
					  'Run RMD with "--authorize" to manually authenticate!' % port
		else:
			url = praw_wrapper.get_reddit_token_url()
		return {
			'url': url,
			'message': message
		}

	@app.route('/api/settings', methods=['GET'])
	def get_settings():
		return settings.to_obj(save_format=False, include_private=False)

	@app.route('/api/settings', methods=['POST'])
	def save_settings():
		settings_obj = request.get_json(silent=True, force=True)
		print('WebUI wants to change settings:', settings_obj)
		# noinspection PyBroadException
		try:
			for k, v in settings_obj.items():
				settings.put(k, v, save_after=False)
			settings.save()
		except Exception:
			import traceback
			traceback.print_exc()
			return False
		return True

	@app.route('/api/sources', methods=['GET'])
	def get_sources():
		ret = {'available': [], 'active': [], 'filters': {}}
		for s in sources.load_sources():
			ret['available'].append(s.to_obj(for_webui=True))
		for s in settings.get_sources():
			ret['active'].append(s.to_obj(for_webui=True))
		ret['filters']['available'] = [f.to_js_obj() for f in filters.get_filters()]
		ret['filters']['operators'] = [f.value for f in filters.Operators]
		return ret

	@app.route('/api/sources', methods=['POST'])
	def save_sources():
		new_obj = request.get_json(silent=True, force=True)
		print('Saving new source list:')
		output_settings = []
		for so in new_obj:
			print('\tType:', so['type'], 'Alias:', so['alias'], so['filters'])
			for s in sources.load_sources():
				if s.type == so['type']:
					s.set_alias(so['alias'])
					for k, v in so['data'].items():
						s.insert_data(k, v)
					for f in so['filters']:
						for fi in filters.get_filters():
							if f['field'] == fi.field:
								fi.set_operator(f['operator'])
								fi.set_limit(f['limit'])
								s.add_filter(fi)
								break
					output_settings.append(s)
		for s in settings.get_sources():
			settings.remove_source(s, save_after=False)
		for s in output_settings:
			settings.add_source(s, prevent_duplicate=False, save_after=False)
		return settings.save()

	@app.route('/api/searchable_fields')
	def api_searchable_fields():
		ret = sql.PostSearcher(self._session).get_searchable_fields()
		return Response(json.dumps(ret), mimetype='application/json')

	@app.route('/api/search')
	def api_search_posts():
		fields = json.loads(request.args.get('fields'))
		term = request.args.get('term')
		page_size = int(request.args.get('page_size'))
		page = int(request.args.get('page'))
		print('Searching posts:', fields, term, page_size, page)
		ret = []
		searcher = sql.PostSearcher(self._session)
		res = searcher.search_fields(fields, term.strip("%"))
		full_len = len(res)
		res = res[page*page_size:page*page_size+page_size]
		for p in res:
			files = []
			for url in p.urls:
				if not url.file:
					print('Post URL Missing a File:', url)
					continue
				file = SanitizedRelFile(base=settings.get("output.base_dir"), file_path=url.file.path)
				if file.is_file():
					files.append({'token': url.file.id, 'path': file.absolute()})
			if len(files):
				ret.append({
					'reddit_id': p.reddit_id,
					'author': p.author,
					'type': p.type,
					'title': p.title,
					'body': p.body,
					'parent_id': p.parent_id,
					'subreddit': p.subreddit,
					'over_18': p.over_18,
					'created_utc': p.created_utc,
					'num_comments': p.num_comments,
					'score': p.score,
					'source_alias': p.source_alias,
					'files': files
				})
		print('returned:', len(ret))
		return {
			'total': full_len,
			'results': ret
		}

	@app.route('/api/shutdown')
	def shutdown():
		""" Terminates Python. """
		sys.exit(0)

	@app.route('/api/download', methods=['PUT'])
	def start_download():
		if self.controller is not None and self.controller.is_running():
			return False
		else:
			self.controller = RMDController()
			self.controller.start()
			self._stat_cache = None
			print('Started downloader.')
			return True

	@app.route('/api/status')
	def download_status():
		if self.controller is None or not self.controller.is_alive():
			return {
				'running': False,
				'summary': self.get_cached_stats()
			}
		return self.controller.get_progress().to_obj()

	@app.route('/api/failed_posts')
	def get_failed():
		fails = self._session \
			.query(sql.Post) \
			.join(sql.URL) \
			.filter(sql.URL.failed == True) \
			.all()
		return Response(json.dumps(sql.encode_safe(fails)), mimetype='application/json')

	@app.route('/api/user')
	def get_authed_user():
		ret = {'user': None}
		# noinspection PyBroadException
		try:
			ret['user'] = praw_wrapper.get_current_username()
		except Exception as ex:
			print(ex)
			pass
		return ret
	return app, socketio


if __name__ == '__main__':
	wui = WebUI()
	print('Started server on http://localhost:8080')
	wui.display()
