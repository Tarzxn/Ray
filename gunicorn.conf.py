import os
bind = '0.0.0.0:' + os.environ.get('PORT', '10000')
workers = 1
worker_class = 'gthread'
threads = 12
timeout = 240
accesslog = '-'
# Never log OAuth authorization codes from URL query strings.
access_log_format = '%(h)s %(m)s %(U)s %(s)s'
