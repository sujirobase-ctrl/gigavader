package com.sujirobase.buidl;

import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.provider.DocumentsContract;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.File;
import java.io.FileInputStream;
import java.io.OutputStream;
import java.util.HashMap;
import java.util.function.BiConsumer;

/** SAF export never overwrites existing user documents or deletes the originals. */
public class ExportFiles {
    public static int copy(Context context, File root, Uri tree, JSONArray files, BiConsumer<Integer,Integer> progress) throws Exception {
        Uri base=DocumentsContract.buildDocumentUriUsingTree(tree,DocumentsContract.getTreeDocumentId(tree));
        HashMap<String,Uri> folders=new HashMap<>();folders.put("",base);int copied=0;
        String rootPath=root.getCanonicalPath()+File.separator;
        for(int i=0;i<files.length();i++){
            JSONObject item=files.getJSONObject(i);File file=new File(item.getString("path"));
            if(!file.getCanonicalPath().startsWith(rootPath))throw new SecurityException("Arquivo fora da pasta do aplicativo");
            String relative=file.getCanonicalPath().substring(rootPath.length());String[] parts=relative.split("/");
            String parent="";Uri directory=base;
            for(int p=0;p<parts.length-1;p++){
                String key=parent+"/"+parts[p];Uri next=folders.get(key);
                if(next==null){next=findDirectory(context,tree,directory,parts[p]);
                    if(next==null)next=DocumentsContract.createDocument(context.getContentResolver(),directory,DocumentsContract.Document.MIME_TYPE_DIR,parts[p]);
                    if(next==null)throw new java.io.IOException("Não foi possível criar a pasta");folders.put(key,next);}
                directory=next;parent=key;
            }
            Uri target=DocumentsContract.createDocument(context.getContentResolver(),directory,item.getString("mime"),file.getName());
            if(target==null)throw new java.io.IOException("Não foi possível criar o arquivo");
            try(FileInputStream input=new FileInputStream(file);OutputStream output=context.getContentResolver().openOutputStream(target,"w")){
                if(output==null)throw new java.io.IOException("Destino indisponível");byte[] buffer=new byte[65536];int length;
                while((length=input.read(buffer))!=-1)output.write(buffer,0,length);
            }catch(Exception error){DocumentsContract.deleteDocument(context.getContentResolver(),target);throw error;}
            copied++;progress.accept(copied,files.length());
        }return copied;
    }
    private static Uri findDirectory(Context context,Uri tree,Uri parent,String name){
        Uri children=DocumentsContract.buildChildDocumentsUriUsingTree(tree,DocumentsContract.getDocumentId(parent));
        try(Cursor cursor=context.getContentResolver().query(children,new String[]{DocumentsContract.Document.COLUMN_DOCUMENT_ID,DocumentsContract.Document.COLUMN_DISPLAY_NAME,DocumentsContract.Document.COLUMN_MIME_TYPE},null,null,null)){
            while(cursor!=null&&cursor.moveToNext())if(name.equals(cursor.getString(1))&&DocumentsContract.Document.MIME_TYPE_DIR.equals(cursor.getString(2)))
                return DocumentsContract.buildDocumentUriUsingTree(tree,cursor.getString(0));
        }return null;
    }
}
