import os
os.environ['EVENTLET_NO_GREENDNS'] = 'yes'

import eventlet  # noqa

eventlet.monkey_patch()
