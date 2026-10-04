package demo.orders;
import java.util.List;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
@Mapper
public interface OrderMapper {
    @Select("SELECT id FROM orders WHERE tenant_id = #{tenantId} AND status = 'PAID' LIMIT #{limit}")
    List<String> findPaid(@Param("tenantId") String tenantId, @Param("limit") int limit);
}
