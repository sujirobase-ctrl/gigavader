package com.sujirobase.buidl;

import android.content.Context;
import android.content.ContentResolver;
import android.content.UriPermission;
import android.database.Cursor;
import android.net.Uri;
import android.provider.DocumentsContract;
import org.json.JSONObject;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.IOException;
import java.util.HashSet;
import java.util.UUID;

/** Storage Access Framework bridge. Only newly-created documents are written. */
public class DestinationStorage {
    private final Context context;
    private final ContentResolver resolver;
    private final File stagingRoot;
    private final HashSet<String> uncommitted = new HashSet<>();
    private static final String DIR = DocumentsContract.Document.MIME_TYPE_DIR;
    private static final String[] COLUMNS = {
        DocumentsContract.Document.COLUMN_DOCUMENT_ID, DocumentsContract.Document.COLUMN_DISPLAY_NAME,
        DocumentsContract.Document.COLUMN_MIME_TYPE, DocumentsContract.Document.COLUMN_SIZE,
        DocumentsContract.Document.COLUMN_FLAGS
    };

    public DestinationStorage(Context context) {
        this.context=context.getApplicationContext(); resolver=context.getContentResolver();
        stagingRoot=new File(context.getFilesDir(),"destinations");
    }
    private Uri tree(String value) throws IOException {
        Uri uri=Uri.parse(value);
        if(!"content".equals(uri.getScheme()) || !DocumentsContract.isTreeUri(uri))
            throw new IOException("Escolha a pasta pelo seletor do Android.");
        return uri;
    }
    private Uri root(Uri tree) { return DocumentsContract.buildDocumentUriUsingTree(tree,DocumentsContract.getTreeDocumentId(tree)); }
    private JSONObject info(Uri document) throws Exception {
        try(Cursor c=resolver.query(document,COLUMNS,null,null,null)) {
            if(c==null)throw new IOException("Não foi possível consultar a pasta de destino.");
            if(!c.moveToFirst())return null;
            return new JSONObject().put("id",c.getString(0)).put("name",c.getString(1)).put("mime",c.getString(2))
                    .put("size",c.isNull(3) ? -1 : c.getLong(3)).put("flags",c.getLong(4));
        }
    }
    public String describe(String value) throws IOException {
        try {
            Uri selected=tree(value); boolean permitted=false;
            for(UriPermission p:resolver.getPersistedUriPermissions()) {
                if(p.getUri().equals(selected) && p.isReadPermission() && p.isWritePermission())permitted=true;
            }
            if(!permitted)throw new IOException("Permissão da pasta perdida. Toque em Escolher pasta de destino novamente.");
            JSONObject folder=info(root(selected));
            if(folder==null || !DIR.equals(folder.optString("mime")))throw new IOException("A pasta foi removida ou está indisponível. Escolha outra pasta.");
            if((folder.optLong("flags") & DocumentsContract.Document.FLAG_DIR_SUPPORTS_CREATE)==0)
                throw new IOException("Esta pasta não permite criar arquivos. Escolha outra pasta.");
            String id=DocumentsContract.getTreeDocumentId(selected);
            return id.startsWith("primary:") ? "Armazenamento interno / "+id.substring(8) : folder.getString("name");
        } catch(IOException e) { throw e; }
        catch(Exception e) { throw new IOException("Sem acesso à pasta escolhida. Selecione-a novamente ou verifique a conexão."); }
    }
    private Uri checkedDocument(String treeValue,String uriValue) throws IOException {
        Uri selected=tree(treeValue), document=tree(uriValue);
        if(!selected.getAuthority().equals(document.getAuthority()) ||
                !DocumentsContract.getTreeDocumentId(selected).equals(DocumentsContract.getTreeDocumentId(document)))
            throw new IOException("Arquivo fora da pasta escolhida.");
        return document;
    }
    public boolean exists(String treeValue,String uriValue,long expected) throws IOException {
        if(uriValue.isEmpty() || expected<=0)return false;
        try {
            JSONObject document=info(checkedDocument(treeValue,uriValue));
            return document!=null && !DIR.equals(document.optString("mime")) && size(Uri.parse(uriValue),document)==expected;
        } catch(java.io.FileNotFoundException e) { return false; }
        catch(Exception e) { throw new IOException("Não foi possível verificar o arquivo na pasta escolhida. Confirme a permissão e tente novamente."); }
    }
    private long size(Uri uri,JSONObject metadata) throws Exception {
        long size=metadata.optLong("size",-1);
        if(size>=0)return size;
        try(InputStream input=resolver.openInputStream(uri)) {
            if(input==null)throw new IOException("Arquivo indisponível");
            long total=0; byte[] buffer=new byte[65536];int count;
            while((count=input.read(buffer))!=-1)total+=count;
            return total;
        }
    }
    private Uri child(Uri selected,Uri parent,String name,boolean directoryOnly) throws Exception {
        Uri children=DocumentsContract.buildChildDocumentsUriUsingTree(selected,DocumentsContract.getDocumentId(parent));
        try(Cursor c=resolver.query(children,COLUMNS,null,null,null)) {
            if(c==null)throw new IOException("Não foi possível listar a pasta.");
            while(c.moveToNext())if(name.equals(c.getString(1)) && (!directoryOnly || DIR.equals(c.getString(2))))
                return DocumentsContract.buildDocumentUriUsingTree(selected,c.getString(0));
        } return null;
    }
    private String availableName(Uri selected,Uri parent,String name) throws Exception {
        if(child(selected,parent,name,false)==null)return name;
        int dot=name.lastIndexOf('.'); String stem=dot>0 ? name.substring(0,dot) : name;
        String suffix=dot>0 ? name.substring(dot) : "";
        // A random suffix prevents overwriting a user's similarly-named file.
        String candidate;
        do { candidate=stem+"-"+UUID.randomUUID().toString().substring(0,8)+suffix; }
        while(child(selected,parent,candidate,false)!=null);
        return candidate;
    }
    public synchronized String publish(String treeValue,String sourcePath,String relative,String mime) throws IOException {
        Uri created=null;
        try {
            describe(treeValue);
            File source=new File(sourcePath).getCanonicalFile();
            if(!source.getPath().startsWith(stagingRoot.getCanonicalPath()+File.separator) || !source.isFile() || source.length()<=0)
                throw new IOException("Temporário de download inválido.");
            String[] parts=relative.split("/",-1);
            if(parts.length<2 || parts.length>64)throw new IOException("Caminho de destino inválido.");
            for(String part:parts)if(part.isEmpty() || part.equals(".") || part.equals("..") || part.contains("\\") || part.indexOf('\0')>=0)
                throw new IOException("Caminho de destino inválido.");
            Uri selected=tree(treeValue), folder=root(selected); StringBuilder actual=new StringBuilder();
            for(int i=0;i<parts.length-1;i++) {
                Uri next=child(selected,folder,parts[i],true);
                if(next==null)next=DocumentsContract.createDocument(resolver,folder,DIR,availableName(selected,folder,parts[i]));
                if(next==null)throw new IOException("Não foi possível criar a subpasta.");
                folder=next; actual.append(info(folder).getString("name")).append('/');
            }
            String temporary=".buidl-"+UUID.randomUUID()+".part";
            created=DocumentsContract.createDocument(resolver,folder,mime,temporary);
            if(created==null)throw new IOException("Não foi possível criar o arquivo.");
            if((info(created).optLong("flags") & DocumentsContract.Document.FLAG_SUPPORTS_RENAME)==0)
                throw new IOException("Este provedor não permite finalizar arquivos com segurança. Escolha outra pasta.");
            long expected=source.length(), copied=0;
            try(FileInputStream input=new FileInputStream(source); OutputStream output=resolver.openOutputStream(created,"wt")) {
                if(output==null)throw new IOException("Destino indisponível");
                byte[] buffer=new byte[65536];int count;
                while((count=input.read(buffer))!=-1) { output.write(buffer,0,count); copied+=count; }
                output.flush();
            }
            if(copied!=expected || size(created,info(created))!=expected)throw new IOException("A gravação ficou incompleta. Verifique o espaço livre.");
            String name=availableName(selected,folder,parts[parts.length-1]);
            Uri renamed=DocumentsContract.renameDocument(resolver,created,name);
            if(renamed==null)throw new IOException("Não foi possível finalizar o arquivo.");
            created=renamed; JSONObject finished=info(created);
            if(finished==null || size(created,finished)!=expected)throw new IOException("O destino não confirmou o arquivo completo.");
            uncommitted.add(created.toString());
            return new JSONObject().put("uri",created.toString()).put("size",expected).put("name",finished.getString("name"))
                    .put("relative",actual+finished.getString("name")).toString();
        } catch(Exception error) {
            if(created!=null)try { DocumentsContract.deleteDocument(resolver,created); } catch(Exception ignored) { }
            String message=error instanceof IOException ? error.getMessage() : "Não foi possível gravar na pasta escolhida. Verifique espaço e permissão.";
            throw new IOException(message+" O temporário local foi mantido para tentar novamente.");
        }
    }
    public synchronized void discard(String treeValue,String uriValue) throws IOException {
        if(!uncommitted.contains(uriValue))throw new IOException("O arquivo não pertence a esta gravação.");
        try {
            DocumentsContract.deleteDocument(resolver,checkedDocument(treeValue,uriValue));
            uncommitted.remove(uriValue);
        } catch(Exception e) { throw new IOException("Não foi possível remover a cópia da gravação interrompida."); }
    }
    public synchronized void commit(String uriValue) { uncommitted.remove(uriValue); }
}
