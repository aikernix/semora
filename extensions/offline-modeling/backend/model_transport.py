"""OpenAI-compatible model delivery for the standalone extension."""
import asyncio
import json
import os
from time import monotonic


class NoDeliveryTimeout(TimeoutError):
    pass


async def read_stream(response, delivered):
    parts = []
    frame = []
    finished = None
    done = False
    total_bytes = 0

    def consume():
        nonlocal finished, done
        data = '\n'.join(frame)
        frame.clear()
        if data == '[DONE]':
            done = True
            return
        event = json.loads(data)
        if 'error' in event:
            raise ValueError('model stream returned an error')
        if not event.get('choices'):
            return
        choice = event['choices'][0]
        content = (choice.get('delta') or {}).get('content')
        if isinstance(content, str) and content:
            if finished is not None:
                raise ValueError('model sent output after finishing')
            delivered.set()
            parts.append(content)
        if choice.get('finish_reason'):
            finished = monotonic()

    lines = response.aiter_lines()
    while not done:
        try:
            if finished is not None:
                async with asyncio.timeout(max(0, finished + .25 - monotonic())):
                    line = await anext(lines)
            else:
                line = await anext(lines)
        except StopAsyncIteration:
            break
        except TimeoutError:
            if finished is None:
                raise
            break
        total_bytes += len(line.encode())
        if total_bytes > 16 * 1024 * 1024:
            raise ValueError('model stream exceeds limit')
        if not line and frame:
            consume()
        elif line.startswith('data:'):
            frame.append(line[5:].lstrip())
    if frame:
        consume()
    if finished is None:
        raise ValueError('incomplete model stream')
    return ''.join(parts)


async def complete_json(client, endpoint, body, request_seconds, delivery_seconds):
    body = dict(body)
    if os.getenv('LLM_STREAM_ENABLED', 'true').lower() == 'true':
        body['stream'] = True
        if os.getenv('LLM_STREAM_INCLUDE_USAGE', 'true').lower() == 'true':
            body['stream_options'] = {'include_usage':True}
    delivered = asyncio.Event()

    async def request():
        async with client.stream('POST',endpoint,json=body) as response:
            response.raise_for_status()
            if 'text/event-stream' in response.headers.get('content-type','').lower():
                content = await read_stream(response, delivered)
            else:
                await response.aread()
                content = response.json()['choices'][0]['message']['content']
            return json.loads(content)

    async with asyncio.timeout(request_seconds):
        if delivery_seconds == 0:
            return await request()
        work = asyncio.create_task(request())
        delivery = asyncio.create_task(delivered.wait())
        try:
            ready, _ = await asyncio.wait((work,delivery),timeout=delivery_seconds,return_when=asyncio.FIRST_COMPLETED)
            if not ready:
                raise NoDeliveryTimeout('no public model delivery')
            return await work
        finally:
            for task in (work,delivery):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work,delivery,return_exceptions=True)
