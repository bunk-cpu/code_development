package source;

import com.google.gson.Gson;
import org.eclipse.jdt.core.JavaCore;
import org.eclipse.jdt.core.compiler.IProblem;
import org.eclipse.jdt.core.dom.*;
import java.nio.file.*;
import java.util.*;

/** Reads an explicit source list; never runs target build scripts or processors. */
public final class Indexer {
    static final List<Map<String,Object>> symbols = new ArrayList<>();
    static final List<Map<String,Object>> relations = new ArrayList<>();
    static final List<Map<String,Object>> problems = new ArrayList<>();
    static String key(IBinding b, String fallback) {
        if (b == null || b.isRecovered()) return fallback;
        return b.getKey();
    }
    static IBinding binding(ASTNode n) {
        if (n instanceof MethodDeclaration m) return m.resolveBinding();
        if (n instanceof AbstractTypeDeclaration t) return t.resolveBinding();
        if (n instanceof VariableDeclarationFragment v) return v.resolveBinding();
        return null;
    }
    static String nodeKey(ASTNode n, String file) {
        return key(binding(n), file + ":" + n.getStartPosition() + ":" + n.getNodeType());
    }
    static ASTNode owner(ASTNode n) {
        for (ASTNode p=n.getParent(); p!=null; p=p.getParent())
            if (p instanceof MethodDeclaration || p instanceof AbstractTypeDeclaration) return p;
        return n;
    }
    static Map<String,Object> loc(CompilationUnit unit, ASTNode n, String file) {
        Map<String,Object> item = new LinkedHashMap<>();
        item.put("file",file);
        item.put("start_line",unit.getLineNumber(n.getStartPosition()));
        item.put("end_line",unit.getLineNumber(n.getStartPosition()+Math.max(0,n.getLength()-1)));
        return item;
    }
    static List<String> modifiers(List<?> list) {
        return list.stream().map(Object::toString).toList();
    }
    public static void main(String[] args) throws Exception {
        // root, response file, sourcepath response file, classpath response file, Java release
        Path root = Path.of(args[0]).toRealPath();
        String[] files = Files.readAllLines(Path.of(args[1])).toArray(String[]::new);
        String[] sourceRoots = Files.readAllLines(Path.of(args[2])).toArray(String[]::new);
        String[] classpath = Files.readAllLines(Path.of(args[3])).toArray(String[]::new);
        ASTParser parser = ASTParser.newParser(AST.getJLSLatest());
        parser.setResolveBindings(true); parser.setBindingsRecovery(true); parser.setStatementsRecovery(true);
        Map<String,String> options = JavaCore.getOptions();
        JavaCore.setComplianceOptions(args.length > 4 ? args[4] : "17",options);
        parser.setCompilerOptions(options);
        parser.setEnvironment(classpath,sourceRoots,null,true);
        parser.createASTs(files,null,new String[0],new FileASTRequestor() {
            @Override public void acceptAST(String source, CompilationUnit unit) {
                String file = root.relativize(Path.of(source)).toString().replace('\\','/');
                for (IProblem p : unit.getProblems()) {
                    if (!p.isError()) continue;
                    problems.add(Map.of("file",file,"line",Math.max(1,p.getSourceLineNumber()),
                                        "message",p.getMessage(),"problem_id",p.getID()));
                }
                unit.accept(new ASTVisitor() {
                    void symbol(ASTNode n, String name, String kind, List<?> mods) {
                        Map<String,Object> item = loc(unit,n,file);
                        IBinding b=binding(n);
                        item.put("key",nodeKey(n,file)); item.put("name",name); item.put("kind",kind);
                        item.put("binding_status",b!=null&&!b.isRecovered()?"RESOLVED":"UNRESOLVED");
                        item.put("modifiers",modifiers(mods));
                        if (n instanceof MethodDeclaration m) {
                            item.put("parameters",m.parameters().stream().map(Object::toString).toList());
                            item.put("signature",m.getName()+"("+String.join(",",m.parameters().stream().map(Object::toString).toList())+")");
                            item.put("return_type",m.getReturnType2()==null?"constructor":m.getReturnType2().toString());
                            item.put("owner",nodeKey(owner(n),file));
                        }
                        if (n instanceof TypeDeclaration t) {
                            item.put("interface",t.isInterface());
                            item.put("super_types",t.superInterfaceTypes().stream().map(Object::toString).toList());
                            if(t.getSuperclassType()!=null) item.put("extends",t.getSuperclassType().toString());
                        }
                        symbols.add(item);
                    }
                    void call(ASTNode n, IMethodBinding b, String label, List<?> arguments, Expression receiver) {
                        Map<String,Object> edge=loc(unit,n,file);
                        edge.put("source_key",nodeKey(owner(n),file));
                        edge.put("target_key",b==null?"":key(b.getMethodDeclaration(),""));
                        edge.put("kind","calls_candidate"); edge.put("name",label);
                        edge.put("resolution_status",b!=null&&!b.isRecovered()?"RESOLVED":"UNRESOLVED");
                        edge.put("arguments",arguments.stream().map(Object::toString).toList());
                        edge.put("receiver",receiver==null?"this":receiver.toString());
                        edge.put("target_type",b==null||b.getDeclaringClass()==null?"":b.getDeclaringClass().getQualifiedName());
                        int mods=b==null?0:b.getModifiers();
                        edge.put("dispatch_basis",Modifier.isStatic(mods)||Modifier.isPrivate(mods)||Modifier.isFinal(mods)?"static_or_final":"declared_receiver_type");
                        edge.put("runtime_verified",false);
                        relations.add(edge);
                    }
                    void typeEdges(ASTNode n, ITypeBinding b) {
                        if (b==null) return;
                        List<ITypeBinding> parents=new ArrayList<>(Arrays.asList(b.getInterfaces()));
                        if(b.getSuperclass()!=null) parents.add(b.getSuperclass());
                        for(ITypeBinding parent:parents) {
                            Map<String,Object> edge=loc(unit,n,file);
                            edge.put("source_key",nodeKey(n,file)); edge.put("target_key",key(parent.getTypeDeclaration(),""));
                            edge.put("kind",parent.isInterface()?"implements":"inherits");
                            edge.put("name",parent.getQualifiedName()); edge.put("resolution_status",parent.isRecovered()?"UNRESOLVED":"RESOLVED");
                            edge.put("runtime_verified",false); relations.add(edge);
                        }
                    }
                    void overrideEdges(MethodDeclaration n) {
                        IMethodBinding b=n.resolveBinding(); if(b==null) return;
                        Set<String> visited=new HashSet<>();
                        Deque<ITypeBinding> types=new ArrayDeque<>();
                        types.add(b.getDeclaringClass());
                        while(!types.isEmpty()) {
                            ITypeBinding t=types.removeFirst(); if(!visited.add(t.getKey())) continue;
                            types.addAll(Arrays.asList(t.getInterfaces()));
                            if(t.getSuperclass()!=null) types.add(t.getSuperclass());
                            for(IMethodBinding method:t.getDeclaredMethods()) if(b.overrides(method)) {
                                Map<String,Object> edge=loc(unit,n,file); edge.put("source_key",nodeKey(n,file));
                                edge.put("target_key",key(method.getMethodDeclaration(),"")); edge.put("kind","overrides");
                                edge.put("name",method.getName()); edge.put("resolution_status","RESOLVED");
                                edge.put("runtime_verified",false); relations.add(edge);
                            }
                        }
                    }
                    @Override public boolean visit(TypeDeclaration n) { symbol(n,n.getName().toString(),"class",n.modifiers()); typeEdges(n,n.resolveBinding()); return true; }
                    @Override public boolean visit(EnumDeclaration n) { symbol(n,n.getName().toString(),"enum",n.modifiers()); return true; }
                    @Override public boolean visit(RecordDeclaration n) { symbol(n,n.getName().toString(),"record",n.modifiers()); return true; }
                    @Override public boolean visit(AnnotationTypeDeclaration n) { symbol(n,n.getName().toString(),"annotation",n.modifiers()); return true; }
                    @Override public boolean visit(MethodDeclaration n) { symbol(n,n.getName().toString(),"method",n.modifiers()); overrideEdges(n); return true; }
                    @Override public boolean visit(VariableDeclarationFragment n) {
                        if (n.getParent() instanceof FieldDeclaration f) symbol(n,n.getName().toString(),"field",f.modifiers());
                        return true;
                    }
                    @Override public boolean visit(MethodInvocation n) { call(n,n.resolveMethodBinding(),n.getName().toString(),n.arguments(),n.getExpression()); return true; }
                    @Override public boolean visit(SuperMethodInvocation n) { call(n,n.resolveMethodBinding(),n.getName().toString(),n.arguments(),null); return true; }
                    @Override public boolean visit(ClassInstanceCreation n) { call(n,n.resolveConstructorBinding(),n.getType().toString(),n.arguments(),n.getExpression()); return true; }
                    @Override public boolean visit(IfStatement n) {
                        Map<String,Object> edge=loc(unit,n,file);
                        edge.put("source_key",nodeKey(owner(n),file)); edge.put("kind","guard");
                        edge.put("condition",n.getExpression().toString()); edge.put("then",n.getThenStatement().toString());
                        edge.put("else",n.getElseStatement()==null?"":n.getElseStatement().toString());
                        edge.put("resolution_status","SYNTAX"); relations.add(edge); return true;
                    }
                    @Override public boolean visit(ImportDeclaration n) {
                        Map<String,Object> edge=loc(unit,n,file); edge.put("source_key",file);
                        edge.put("kind","imports"); edge.put("name",n.getName().toString());
                        edge.put("resolution_status",n.resolveBinding()==null?"UNRESOLVED":"RESOLVED");
                        relations.add(edge); return true;
                    }
                });
            }
        },null);
        System.out.println(new Gson().toJson(Map.of("analyzer","eclipse-jdt-3.42.0","symbols",symbols,"relations",relations,"problems",problems)));
    }
}
