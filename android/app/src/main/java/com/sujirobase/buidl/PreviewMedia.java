package com.sujirobase.buidl;

import android.graphics.Bitmap;
import android.media.MediaDataSource;
import android.media.MediaMetadataRetriever;
import android.os.Build;
import android.util.Base64;
import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import java.io.IOException;
import java.io.ByteArrayOutputStream;
import java.util.ArrayList;
import java.util.List;

/** Native codecs with Python's domain-scoped cookies and bounded HTTP ranges. */
public final class PreviewMedia extends MediaDataSource {
    private final PyObject source;
    private boolean closed;
    public PreviewMedia(AppModel model, int queue, int index, boolean cover) throws IOException {
        try { source=Python.getInstance().getModule("mobile_media").callAttr("MediaSource",model.engine,queue,index,cover); }
        catch(Exception e) { throw new IOException("Prévia indisponível",e); }
    }
    @Override public synchronized int readAt(long position, byte[] buffer, int offset, int size) throws IOException {
        if(closed)throw new IOException("Prévia encerrada");
        if(size==0)return 0;
        try { byte[] data=source.callAttr("read_at",position,size).toJava(byte[].class);
            if(data.length==0)return -1; int count=Math.min(size,data.length);System.arraycopy(data,0,buffer,offset,count);return count;
        } catch(Exception e){throw new IOException("Limite ou falha da prévia",e);}
    }
    @Override public synchronized long getSize() throws IOException {return source.callAttr("get_size").toLong();}
    @Override public synchronized void close() {if(!closed){closed=true;source.callAttr("close");}}

    public static Bitmap cover(AppModel model,int queue,int index) throws Exception {
        synchronized(model.videoLock) {
            try(PreviewMedia source=new PreviewMedia(model,queue,index,true)) {
                MediaMetadataRetriever codec=new MediaMetadataRetriever();
                try {
                    codec.setDataSource(source);
                    int w=Integer.parseInt(codec.extractMetadata(MediaMetadataRetriever.METADATA_KEY_VIDEO_WIDTH));
                    int h=Integer.parseInt(codec.extractMetadata(MediaMetadataRetriever.METADATA_KEY_VIDEO_HEIGHT));
                    if(w<=0||h<=0||(long)w*h>8_294_400)throw new IOException("Resolução de prévia excedida");
                    if(android.os.Build.VERSION.SDK_INT>=27)return codec.getScaledFrameAtTime(0,MediaMetadataRetriever.OPTION_CLOSEST_SYNC,480,360);
                    Bitmap raw=codec.getFrameAtTime(0,MediaMetadataRetriever.OPTION_CLOSEST_SYNC);
                    if(raw==null)return null;
                    float scale=Math.min(480f/raw.getWidth(),360f/raw.getHeight());
                    Bitmap small=Bitmap.createScaledBitmap(raw,Math.max(1,Math.round(raw.getWidth()*scale)),Math.max(1,Math.round(raw.getHeight()*scale)),true);
                    if(small!=raw)raw.recycle();return small;
                } finally {codec.release();}
            }
        }
    }

    public static List<String> frames(AppModel model,int queue,int index) throws Exception {
        synchronized(model.videoLock) {
            try(PreviewMedia source=new PreviewMedia(model,queue,index,false)) {
                MediaMetadataRetriever codec=new MediaMetadataRetriever();List<String> out=new ArrayList<>();
                try {
                    codec.setDataSource(source);
                    int w=Integer.parseInt(codec.extractMetadata(MediaMetadataRetriever.METADATA_KEY_VIDEO_WIDTH));
                    int h=Integer.parseInt(codec.extractMetadata(MediaMetadataRetriever.METADATA_KEY_VIDEO_HEIGHT));
                    if(w<=0||h<=0||(long)w*h>8_294_400)throw new IOException("Resolução de prévia excedida");
                    long duration=Long.parseLong(codec.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION));
                    int count=(int)Math.min(80,Math.max(1,Math.min(80,(duration+124)/125)));
                    for(int i=0;i<count;i++) {
                        Bitmap frame;
                        long time=i*125_000L;
                        if(Build.VERSION.SDK_INT>=27)frame=codec.getScaledFrameAtTime(time,MediaMetadataRetriever.OPTION_CLOSEST_SYNC,480,360);
                        else frame=codec.getFrameAtTime(time,MediaMetadataRetriever.OPTION_CLOSEST_SYNC);
                        if(frame==null)continue;
                        float scale=Math.min(1f,Math.min(480f/frame.getWidth(),360f/frame.getHeight()));
                        Bitmap small=scale<1f?Bitmap.createScaledBitmap(frame,Math.max(1,Math.round(frame.getWidth()*scale)),Math.max(1,Math.round(frame.getHeight()*scale)),true):frame;
                        try(ByteArrayOutputStream bytes=new ByteArrayOutputStream()){
                            small.compress(Bitmap.CompressFormat.JPEG,70,bytes);
                            out.add("data:image/jpeg;base64,"+Base64.encodeToString(bytes.toByteArray(),Base64.NO_WRAP));
                        }
                        if(small!=frame)small.recycle();frame.recycle();
                    }
                    return out;
                }finally{codec.release();}
            }
        }
    }
}
