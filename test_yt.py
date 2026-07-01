from youtube_transcript_api import YouTubeTranscriptApi
import asyncio

async def main():
    video_id = 'dQw4w9WgXcQ'
    languages = ['zh-Hans', 'zh-Hant', 'en', 'ja', 'ko']
    if hasattr(YouTubeTranscriptApi, 'get_transcript'):
        transcript_list = await asyncio.to_thread(
            YouTubeTranscriptApi.get_transcript, 
            video_id, 
            languages=languages
        )
        text = " ".join([i['text'] for i in transcript_list])
    else:
        api = YouTubeTranscriptApi()
        transcript_list = await asyncio.to_thread(
            api.fetch, 
            video_id, 
            languages=languages
        )
        text = " ".join([i.text for i in transcript_list])
    print(text[:50])

asyncio.run(main())
