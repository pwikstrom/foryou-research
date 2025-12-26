# -*- coding: utf-8 -*-
"""
Script Name: 
Description: 
Author: Patrik based on PykTok code by freelon
Date: Thu Jul 14 14:06:01 2022
"""

import browser_cookie3





headers = {'Accept-Encoding': 'gzip, deflate, sdch',
           'Accept-Language': 'en-US,en;q=0.8',
           'Upgrade-Insecure-Requests': '1',
           'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/56.0.2924.87 Safari/537.36',
           'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
           'Cache-Control': 'max-age=0',
           'Connection': 'keep-alive'}
runsb_err = 'No browser defined for cookie extraction. We strongly recommend you run \'specify_browser\', which takes as its sole argument a string representing a browser installed on your system, e.g. "chrome," "firefox," "edge," etc.'






class BrowserNotSpecifiedError(Exception):
    def __init__(self):
        super().__init__(runsb_err)





def specify_browser(browser):
    global cookies
    cookies = getattr(browser_cookie3, browser)(domain_name='www.tiktok.com')






def generate_data_row(item_struct):
    from copy import copy
    from pandas import DataFrame, to_datetime
    from datetime import datetime


    pyk_data_defaults = {
        'desc': "",
        'createTime': "no default", # the type is datetime, but that doesn't work for me but aok, since I'm not using it anyway
        'item_id': "",
        'video_duration': -1,
        'image_list': "",
        'author_id': "",
        'author_uniqueId': "",
        'author_nickname': "",
        'author_signature': "",
        'author_verified': False,
        'music_id': "",
        'music_title': "",
        'music_authorName': "",
        'music_album': "",
        'music_original': False,
        'music_duration': 0,
        'playlistId': "",
        'stats_diggCount': -1,
        'stats_commentCount': -1,
        'stats_playCount': -1,
        'stats_collectCount': -1,
        'stats_shareCount': -1,
        'anchors': "",
        'challenges': "",
        'poi_name': "",
        'poi_address': "",
        'poi_city': "",
        'poi_province': "",
        'poi_country': "",
        'IsAigc': False,
        'AIGCDescription': "",
        'aigcLabelType': "",
        'isAd': False,
        #'video_cover': "",
        'video_downloaded': False,
        #'audio_extracted': False,
        #'cover_downloaded': False,
        #'do_not_modify': True,
        'last_modified': "no default" # the type is datetime, but that doesn't work for me but aok, since I'm not using it anyway
    }

    item_struct["item_id"] = str(item_struct["id"])

    try:
        item_struct["createTime"] = datetime.fromtimestamp(int(item_struct["createTime"]))
    except:
        item_struct["createTime"] = datetime(2000,1,1)

    item_struct["last_modified"] = datetime.now()

    if "video" in item_struct.keys():
        for k in ["duration"]:
            if k in item_struct["video"].keys():
                item_struct["video_"+k] = item_struct["video"][k]

    image_list = []
    if "imagePost" in item_struct.keys():
        for k in item_struct['imagePost']['images']:
            image_list += [k['imageURL']['urlList'][0]]
    if len(image_list) > 0:
        item_struct['image_list'] = " | ".join(image_list)

    for k in ["id","uniqueId","nickname","signature","verified"]:
        if k in item_struct["author"].keys():
            item_struct["author_"+k] = item_struct["author"][k]

    if "music" in item_struct.keys():
        for k in ["id","title","authorName","original","duration","album"]:
            if k in item_struct["music"].keys():
                item_struct["music_"+k] = item_struct["music"][k]

    if "challenges" in item_struct.keys():
        item_struct["challenges"] = "|".join([f"[{g['id']}]{g['title']}" for g in item_struct["challenges"]])
    
    if "anchors" in item_struct.keys() and isinstance(item_struct["anchors"],list) and \
        len(item_struct["anchors"]) > 0 and "description" in item_struct["anchors"][0].keys():
            item_struct["anchors"] = "|".join([f"[{g['id']}]{g['description']}" for g in item_struct["anchors"]])

    if "poi" in item_struct.keys():
        for k in ["name","address","city","province","country"]:
            if k in item_struct["poi"].keys():
                item_struct["poi_"+k] = item_struct["poi"][k]

    if "stats" in item_struct.keys():
        for k in ["diggCount","shareCount","commentCount","playCount","collectCount"]:
            if k in item_struct["stats"].keys():
                item_struct["stats_"+k] = int(item_struct["stats"][k])

    pyk_data_types = {}
    for dh in pyk_data_defaults.keys():
        if not dh in ["createTime","last_modified"]:
            pyk_data_types[dh] = type(pyk_data_defaults[dh])
            if dh not in item_struct.keys():
                item_struct[dh] = pyk_data_defaults[dh]

    # converting to DataFrame
    pyk_data = DataFrame.from_dict(item_struct,orient="index").T
    pyk_data.reset_index(drop=True, inplace=True)
    if len(pyk_data) > 1:
        print("ERROR - generated multiple rows. Should only be a single row. Only keeping top row")
        pyk_data = pyk_data.loc[[0],:].copy()
    
    # fixing date types
    pyk_data.loc[0,"createTime"] = to_datetime(pyk_data.loc[0,"createTime"])
    pyk_data.loc[0,"last_modified"] = to_datetime(pyk_data.loc[0,"last_modified"])
    
    # sorting the columns and assuring the types
    pyk_data = pyk_data[list(pyk_data_defaults.keys())] 
    pyk_data = pyk_data.astype(pyk_data_types)

    return pyk_data





def alt_get_tiktok_json(video_url,
                        browser_name=None,
                        verbose=False):
    from bs4 import BeautifulSoup
    from requests import get as requests_get
    from json import loads
    from time import sleep

    if 'cookies' not in globals() and browser_name is None:
        raise BrowserNotSpecifiedError
    global cookies
    if browser_name is not None:
        cookies = getattr(browser_cookie3, browser_name)(
            domain_name='www.tiktok.com')

    try:
        tt = requests_get(video_url,
                          headers=headers,
                          cookies=cookies,
                          timeout=20)

    except Exception as e:
        if verbose:
            print(f"ERROR (PykTok)-1", end="  |  ",flush=True)
            print(f". Failed to download the json of {video_url}. Sleeping for 3s and then returning None.")
        sleep(3)
        return None
    
    # retain any new cookies that got set in this request
    cookies = tt.cookies
    soup = BeautifulSoup(tt.text, "html.parser")
    tt_script = soup.find(
        'script', attrs={'id': "__UNIVERSAL_DATA_FOR_REHYDRATION__"})
        #'script', attrs={'id':"SIGI_STATE"})
    
    tt_json = loads(tt_script.string)
    
    return tt_json



def save_tiktok(video_url,
                save_video=True,
                max_duration_to_save=9000, # we're not going to download videos that are longer than 15 minutes. Prob still waaaay too long
                browser_name=None,
                save_path="",
                stream_to_bucket=None,
                verbose=False):
    
    from os.path import join
    from pandas import DataFrame
    from requests import get as requests_get
    from time import sleep

    _CHUNK = 8 * 1024 * 1024        # 8 MiB, multiple of 256 KiB ≈ optimal for GCS
    verbose=False

    if 'cookies' not in globals() and browser_name is None:
        raise BrowserNotSpecifiedError

    else:
        try:
            tt_json = alt_get_tiktok_json(video_url,
                                          browser_name=browser_name,
                                          verbose=verbose)
            

        except Exception as e:
            if verbose:
                print(f"ERROR (PykTok)-2", end="  |  ",flush=True)
                print(f"Could not retrieve a json for {video_url} . {e}")
            return DataFrame()
        
        if tt_json:
            try:
                data_slot = tt_json["__DEFAULT_SCOPE__"]['webapp.video-detail']['itemInfo']['itemStruct']
                data_row = generate_data_row(data_slot)
                data_row.loc[0,'video_downloaded'] = False

            except Exception as e:
                if verbose:
                    print(f"ERROR (PykTok)-3", end="  |  ",flush=True)
                    print(f". 'itemStruct' is corrupted ot missing in the json for {video_url} . {e}")
                return DataFrame()

            video_id = video_url.rstrip('/').split('/')[-1]

            if save_video:
                if data_row.loc[0,'video_duration'] <= max_duration_to_save:

                    if len(data_row.loc[0,'image_list']) == 0:
                        video_fn = f"{video_id}.mp4"

                        if len(tt_json["__DEFAULT_SCOPE__"]['webapp.video-detail']['itemInfo']['itemStruct']['video']) == 0:
                            if verbose:
                                print(f"ERROR (PykTok)-4", end="  |  ",flush=True)
                                print(f". The video metadata in the json for {video_url} came back empty.")
                            return DataFrame()

                        tt_download_video_url = tt_json["__DEFAULT_SCOPE__"]['webapp.video-detail']['itemInfo']['itemStruct']['video']['playAddr']
                        if tt_download_video_url == '':
                            tt_download_video_url = tt_json["__DEFAULT_SCOPE__"]['webapp.video-detail']['itemInfo']['itemStruct']['video']['downloadAddr']
                        if len(tt_download_video_url) == 0 or tt_download_video_url is None or not tt_download_video_url.startswith("http"):
                            if verbose:
                                print(f"WARNING (PykTok)-1", end="  |  ",flush=True)
                                print(f". The download url in the json for {video_url} is empty or corrupted. Returning the metadata with video_downloaded = False. <<{tt_download_video_url}>>")
                            return data_row

                        try:
                            headers['referer'] = 'https://www.tiktok.com/'
                            if stream_to_bucket is None:
                                tt_video = requests_get(
                                    tt_download_video_url,
                                    allow_redirects=True,
                                    headers=headers,
                                    cookies=cookies)
                                with open(join(save_path,video_fn), 'wb') as fn:
                                    fn.write(tt_video.content)
                            else:
                                tt_video = requests_get(
                                    tt_download_video_url,
                                    headers=headers,
                                    cookies=cookies,
                                    stream=True,
                                    timeout=60)

                                video_blob = stream_to_bucket.blob(video_fn)
                                with video_blob.open("wb") as gcs_file:
                                    for chunk in tt_video.iter_content(chunk_size=_CHUNK):
                                        if chunk:                       # skip keep-alive chunks
                                            gcs_file.write(chunk)


                            data_row.loc[0,'video_downloaded'] = True
                        except Exception as e:
                            if verbose:
                                print(f"WARNING (PykTok)-2", end="  |  ",flush=True)
                                print(f". Failed to download the video of {video_url}. Sleeping for 3s and then returning the metadata with video_downloaded = False.")
                            sleep(3)
                            return data_row
                    else:
                        image_list = data_row.loc[0,'image_list'].split(" | ")
                        try:
                            for k,one_image in enumerate(image_list):
                                image_fn = f"{video_id}_{k+1:02}.jpeg"
                                headers['referer'] = 'https://www.tiktok.com/'
                                if stream_to_bucket is None:
                                    tt_image = requests_get(
                                        one_image,
                                        allow_redirects=True,
                                        headers=headers,
                                        cookies=cookies)
                                    with open(join("",image_fn), 'wb') as fn:
                                        fn.write(tt_image.content)
                                else:
                                    tt_image = requests_get(
                                        one_image,
                                        headers=headers,
                                        cookies=cookies,
                                        stream=True,
                                        timeout=60)

                                    image_blob = stream_to_bucket.blob(image_fn)
                                    with image_blob.open("wb") as gcs_file:
                                        for chunk in tt_image.iter_content(chunk_size=_CHUNK):
                                            if chunk:                       # skip keep-alive chunks
                                                gcs_file.write(chunk)

                            data_row.loc[0,'video_downloaded'] = True
                        except Exception as e:
                            if verbose:
                                print(f"WARNING (PykTok)-2b", end="  |  ",flush=True)
                                print(f". Failed to download an image of '{video_id}'. Sleeping for 3s and then returning the metadata with video_downloaded = False.")
                            sleep(3)
                            return data_row
                else:
                    if verbose:
                        print(f"Video '{video_id}' has a duration ({data_row.loc[0,'video_duration']:,}s) that exceeds {max_duration_to_save:,}s. Not downloading this video.")
            else:
                pass


        else:
            if verbose:
                print(f"ERROR (PykTok)-5", end="  |  ",flush=True)
                print(f". Could not retrieve a json for {video_url}.")
            return DataFrame()
    
    return data_row
