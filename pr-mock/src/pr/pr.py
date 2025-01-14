import ssl
import logging
import json

import aiohttp
from aiohttp import web

import pr

import livemetrics
import livemetrics.publishers.aiohttp

routes = web.RouteTableDef()

def is_healthy():
    return True

LM = livemetrics.LiveMetrics(json.dumps(dict(version=pr.__version__)), "pr", is_healthy)

def ok_status(ret):
    return str(ret.status)


# _____________________________________________________________________________
@web.middleware
async def error_middleware(request, handler):
    try:
        response = await handler(request)
        if response.status >= 400:
            logging.info("Request failed with HTTP error %d", response.status)
        return response
    except aiohttp.web_exceptions.HTTPException:
        raise
    except Exception as exc:
        logging.exception("Exception caught in middleware: [%s]", str(exc))
        return web.json_response(dict(code=0, message=str(exc)), status=500)


# _____________________________________________________________________________
def get_ssl_context():
    ctx = None
    if pr.args.server_certfile:
        logging.debug("Setup SSL context for this server: %s - %s", pr.args.server_certfile, pr.args.server_keyfile)
        if pr.args.server_ca_certfile:
            # used ssl.CERT_REQUIRED for mutual authent needed, ssl.CERT_OPTIONAL if not
            logging.debug("SSL context setup for cafile: %s", pr.args.server_ca_certfile)
            ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH,
                                            cafile=pr.args.server_ca_certfile)
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.check_hostname = True
        else:
            ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(pr.args.server_certfile,
                            pr.args.server_keyfile,
                            password=pr.args.server_keyfile_password)
        logging.debug("certfile/keyfile loaded for SSL Context")
    return ctx


# _____________________________________________________________________________
async def _strip_server(req, res):
    if 'Server' in res.headers:
        del res.headers['Server']


# _____________________________________________________________________________
async def start_monitoring(app):
    app2 = web.Application(middlewares=[error_middleware])
    app2.add_routes(livemetrics.publishers.aiohttp.routes(LM))
    runner = web.AppRunner(app2)
    await runner.setup()
    site = web.TCPSite(runner, host=pr.args.ip, port=pr.args.monitoring_port)
    await site.start()

# _____________________________________________________________________________
def get_app():
    app = web.Application(client_max_size=pr.args.input_max_size*1024*1024,
                          middlewares=[error_middleware])
    app.add_routes(routes)
    if pr.args.monitoring_port<=0 or pr.args.monitoring_port==pr.args.port:
        app.add_routes(livemetrics.publishers.aiohttp.routes(LM))
    else:
        app.on_startup.append(start_monitoring)
    # Remove Server header for security reason
    app.on_response_prepare.append(_strip_server)

    return app


# _____________________________________________________________________________
def serve():
    app = get_app()
    if pr.args.do_not_start:
        logging.warning('Not starting the application')
        return
    logging.info('Starting application...')
    web.run_app(app, host=pr.args.ip, port=pr.args.port, access_log=None, ssl_context=get_ssl_context())

PERSONS = {}

# _____________________________________________________________________________
# PR interface
# _____________________________________________________________________________

# _____________________________________________________________________________
@routes.post('/v1/persons/{personId}')
@LM.timer("createPerson", ok_status, "error")
async def createPerson(request):
    transaction_id = request.query['transactionId']
    person_id = request.match_info['personId']

    data = await request.json()
    logging.info("[%s] - createPerson for personId [%s]", transaction_id, person_id)
    if person_id in PERSONS:
        return web.Response(status=409)

    PERSONS[person_id] = data
    PERSONS[person_id]['identities'] = []
    return web.Response(status=201)

# _____________________________________________________________________________
@routes.post('/v1/persons/{personId}/identities/{identityId}')
@LM.timer("createIdentityWithId", ok_status, "error")
async def createIdentityWithId(request):
    transaction_id = request.query['transactionId']
    person_id = request.match_info['personId']
    identity_id = request.match_info['identityId']

    data = await request.json()
    logging.info("[%s] - createIdentityWithId for personId [%s]/[%s]", transaction_id, person_id, identity_id)
    p = PERSONS.get(person_id, None)
    if p is None:
        return web.Response(status=404)

    for i in p['identities']:
        if i['identityId'] == identity_id:
            return web.Response(status=409)

    p['identities'].append (data)
    p['identities'][-1]['identityId'] = identity_id
    return web.Response(status=201)

# _____________________________________________________________________________
# Data Access interface
# _____________________________________________________________________________

# _____________________________________________________________________________
@routes.post('/v1/persons/{uin}/match')
@LM.timer("matchPersonAttributes", ok_status, "error")
async def matchPersonAttributes(request):
    uin = request.match_info['uin']

    data = await request.json()
    logging.info("matchPersonAttributes for UIN [%s]", uin)

    # check person exists and has an identity
    p = PERSONS.get(uin, None)
    if p is None:
        return web.Response(status=404)
    if len(p['identities'])<1:
        return web.Response(status=404)
    i = p['identities'][0]
    ret = []
    for k, v in data.items():
        if k not in i['biographicData']:
            ret.append(dict(attributeName=k, errorCode=0))
        elif i['biographicData'][k] != v:
            ret.append(dict(attributeName=k, errorCode=1))
    return web.json_response(ret, status=200)


# _____________________________________________________________________________
@routes.get('/v1/persons')
@LM.timer("queryPersonList", ok_status, "error")
async def queryPersonList(request):
    offset = int(request.query.get('offset', 0))
    limit = int(request.query.get('limit', 100))
    names = request.query.getall('names', [])
    attributes = {}
    for k, v in request.query.items():
        if k in ['names', 'offset', 'limit']:
            continue
        attributes[k] = v
    logging.info("queryPersonList for attributes [%s]", attributes)

    ret = []
    for uin, p in PERSONS.items():
        if len(p['identities'])<1:
            continue
        for i in p['identities']:
            x = 1
            for k, v in attributes.items():
                if k not in i['biographicData'] or i['biographicData'][k] != v:
                    x = x*0
            if x==1:
                ret.append(uin)
                break
    
    ret = ret[offset:offset+limit]

    if len(names)==0:
        return web.json_response(ret, status=200)
    
    ret2 = []
    for uin in ret:
        p = PERSONS.get(uin)
        i = p['identities'][-1]
        r2 = {}
        for k in names:
            if k in i['biographicData']:
                r2[k] = i['biographicData'][k]
        ret2.append(r2)
    return web.json_response(ret2, status=200)


# _____________________________________________________________________________
@routes.get('/v1/persons/{uin}')
@LM.timer("readPersonAttributes", ok_status, "error")
async def readPersonAttributes(request):
    uin = request.match_info['uin']
    names = request.query.getall('attributeNames', [])

    logging.info("readPersonAttributes for UIN [%s]", uin)

    # check person exists and has an identity
    p = PERSONS.get(uin, None)
    if p is None:
        return web.Response(status=404)

    i = p['identities'][-1]
    ret = {}
    for k in names:
        if k in i['biographicData']:
            ret[k] = i['biographicData'][k]
    return web.json_response(ret, status=200)

