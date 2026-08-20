# prompt_temp
- - -



let's work on better defining behavior for using the MP4 proxies over the raw MOV files.


for each video file encountered, see if the same file name (but different extension) exists.

- if they do, compare them via length (with plus or minus 0.1s buffer) or frame count (pick whatever plus minus buffer is appropriate here) to verify they are the same footage.

  - if they are, prefer using the smaller sized file to extract the still images from.

- if there is only one file with a given name, do the following:

  - run ffmpeg or ffprobe to examine the metadata.
    - if ffmpeg can decode the video to extract images, do that.
    - if not, such as for ProRes, ProRes RAW, or a camera manufacturer's own raw format (X-OCN, N-Raw, Canon raw, etc.) extract and convert with qlmanage/sip (qlmanage makes a PNG, sip converts to JPEG)
if the video cannot be decoded by any of the above tools, print an error for it, mark it as errored in the JSON plan file and move onto the next file








what are the Apple-included CLI tools that were useful for looking at ProRes and ProRes RAW clips that ffmpeg couldn't decode?